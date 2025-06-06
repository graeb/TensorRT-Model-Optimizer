# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import copy
import random
import time
import warnings

import numpy as np
import torch
from accelerate.hooks import remove_hook_from_module
from example_utils import (
    get_model,
    get_model_type,
    get_processor,
    get_tokenizer,
    is_enc_dec,
    is_model_on_gpu,
)
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

import modelopt.torch.opt as mto
import modelopt.torch.quantization as mtq
import modelopt.torch.sparsity as mts
from modelopt.torch.export import export_hf_checkpoint, export_tensorrt_llm_checkpoint
from modelopt.torch.utils.dataset_utils import (
    create_forward_loop,
    get_dataset_dataloader,
    get_max_batch_size,
)
from modelopt.torch.utils.image_processor import MllamaImageProcessor
from modelopt.torch.utils.memory_monitor import launch_memory_monitor
from modelopt.torch.utils.vlm_dataset_utils import get_vlm_dataset_dataloader

RAND_SEED = 1234

QUANT_CFG_CHOICES = {
    "int8": "INT8_DEFAULT_CFG",
    "int8_sq": "INT8_SMOOTHQUANT_CFG",
    "fp8": "FP8_DEFAULT_CFG",
    "int4_awq": "INT4_AWQ_CFG",
    "w4a8_awq": "W4A8_AWQ_BETA_CFG",
    "nvfp4": "NVFP4_DEFAULT_CFG",
    "nvfp4_awq": "NVFP4_AWQ_LITE_CFG",
}

mto.enable_huggingface_checkpointing()


def auto_quantize(
    model, qformat, auto_quantize_bits, calib_dataloader, calibrate_loop, batch_size=1
):
    qformat_list = qformat.split(",")
    # Check if all provided quantization formats are supported
    if args.export_fmt == "hf":
        assert all(
            qformat in ["fp8", "int4_awq", "nvfp4", "nvfp4_awq", "w4a8_awq"]
            for qformat in qformat_list
        ), (
            "One or more quantization formats provided are not supported for unified checkpoint export"
        )
    else:
        assert all(
            qformat in ["fp8", "int8_sq", "int4_awq", "w4a8_awq", "nvfp4", "nvfp4_awq"]
            for qformat in qformat_list
        ), "One or more quantization formats provided are not supported for tensorrt llm export"

    model, _ = mtq.auto_quantize(
        model,
        constraints={"effective_bits": auto_quantize_bits},
        data_loader=calib_dataloader,
        forward_step=lambda model, batch: model(**batch),
        loss_func=lambda output, data: output.loss,
        quantization_formats=[QUANT_CFG_CHOICES[format] for format in qformat_list]
        + [None],  # TRTLLM only support one quantization format or None
        num_calib_steps=len(calib_dataloader),
        num_score_steps=min(
            len(calib_dataloader), 128 // batch_size
        ),  # Limit the number of score steps to avoid long calibration time
        verbose=True,
        disabled_layers=["*lm_head*"],
    )

    # We need to explicitly calibrate for kv cache quantization
    enable_kv_cache_quantization = "int8" not in args.qformat and not args.disable_kv_cache_quant
    print(f"{'Enable' if enable_kv_cache_quantization else 'Disable'} KV cache quantization")
    if enable_kv_cache_quantization:
        mtq.set_quantizer_by_cfg(
            model,
            quant_cfg={"*output_quantizer": {"num_bits": (4, 3), "axis": None, "enable": True}},
        )
        # Lets calibrate only the output quantizer this time. Let's disable all other quantizers.
        with mtq.set_quantizer_by_cfg_context(
            model, {"*": {"enable": False}, "*output_quantizer": {"enable": True}}
        ):
            mtq.calibrate(model, algorithm="max", forward_loop=calibrate_loop)
    return model


def quantize_model(model, quant_cfg, args, calib_dataloader=None):
    # The calibration loop for the model can be setup using the modelopt API.
    #
    # Example usage:
    # from modelopt.torch.utils.dataset_utils import create_forward_loop
    # model = ...  # Initilaize the model
    # tokenizer = ...  # Initilaize the tokenizer
    # quant_cfg = ...  # Setup quantization configuration
    # forward_loop = create_forward_loop(model=model, dataset_name="cnn_dailymail", tokenizer=tokenizer)
    # mtq.quantize(model, quant_cfg, forward_loop=forward_loop)

    # The calibrate_loop is a custom defined method to run the model with the input data.
    # The basic version looks like:
    #
    # def calibrate_loop(model, dataloader):
    #     for data in dataloader:
    #         model(**data)
    #
    # We also provided a util method to generate the forward_loop with additional error handlings.

    calibrate_loop = create_forward_loop(dataloader=calib_dataloader)

    assert not (args.auto_quantize_bits and args.inference_pipeline_parallel > 1), (
        "Auto Quantization is not supported for pipeline parallel size > 1"
    )

    print("Starting quantization...")
    start_time = time.time()
    if args.auto_quantize_bits:
        model = auto_quantize(
            model,
            args.qformat,
            args.auto_quantize_bits,
            calib_dataloader,
            calibrate_loop,
            args.batch_size,
        )
    else:
        model = mtq.quantize(model, quant_cfg, forward_loop=calibrate_loop)
    end_time = time.time()
    print(f"Quantization done. Total time used: {end_time - start_time}s")
    return model


def main(args):
    if not torch.cuda.is_available():
        raise EnvironmentError("GPU is required for inference.")

    random.seed(RAND_SEED)
    np.random.seed(RAND_SEED)

    # launch a memory monitor to read the currently used GPU memory.
    launch_memory_monitor()

    # Check that only one quantization format is provided for non auto_quant case
    if not args.auto_quantize_bits:
        assert len(args.qformat.split(",")) == 1, (
            "Quantization supports only one quantization format."
        )

    # Check arguments for unified_hf export format and set to default if unsupported arguments are provided
    if args.export_fmt == "hf":
        assert args.sparsity_fmt == "dense", (
            f"Sparsity format {args.sparsity_fmt} not supported by unified export api."
        )

        if not args.auto_quantize_bits:
            assert args.qformat in [
                "int4_awq",
                "fp8",
                "nvfp4",
                "nvfp4_awq",
                "w4a8_awq",
            ], f"Quantization format {args.qformat} not supported for HF export path"

    model = get_model(args.pyt_ckpt_path, args.device, trust_remote_code=args.trust_remote_code)
    model_type = get_model_type(model)

    device = model.device
    if hasattr(model, "model"):
        device = model.model.device
    processor = None
    tokenizer = None
    if model_type == "mllama":
        if args.dataset is None:
            args.dataset = "scienceqa"
            warnings.warn(
                "Currently only the scienceqa dataset is supported for the mllama model. "
                "Overriding dataset to scienceqa."
            )
        elif args.dataset != "scienceqa":
            raise ValueError("Only the scienceqa dataset is supported for the mllama model.")
        processor = get_processor(
            args.pyt_ckpt_path, device, trust_remote_code=args.trust_remote_code
        )
    else:
        if args.dataset is None:
            args.dataset = "cnn_dailymail"
            warnings.warn("No dataset specified. Defaulting to cnn_dailymail.")
        tokenizer = get_tokenizer(args.pyt_ckpt_path, trust_remote_code=args.trust_remote_code)
        default_padding_side = tokenizer.padding_side
        # Left padding usually provides better calibration result.
        tokenizer.padding_side = "left"

    if args.sparsity_fmt != "dense":
        if args.batch_size == 0:
            # Sparse algorithm takes more GPU memory so we reduce the batch_size by 4.
            args.batch_size = max(get_max_batch_size(model) // 4, 1)
            if args.batch_size > args.calib_size:
                args.batch_size = args.calib_size

        print(f"Use calib batch_size {args.batch_size}")

        # Different calibration datasets are also available, e.g., "pile" and "wikipedia"
        # Please also check the docstring for the datasets available
        assert tokenizer is not None and isinstance(
            tokenizer, (PreTrainedTokenizer, PreTrainedTokenizerFast)
        ), "The PreTrainedTokenizer must be set"
        calib_dataloader = get_dataset_dataloader(
            dataset_name=args.dataset,
            tokenizer=tokenizer,
            batch_size=args.batch_size,
            num_samples=args.calib_size,
            device=device,
        )
        model = mts.sparsify(
            model,
            args.sparsity_fmt,
            config={"data_loader": calib_dataloader, "collect_func": lambda x: x},
        )
        mts.export(model)

    if (
        not args.auto_quantize_bits
        and args.qformat in ["fp8", "int8_sq", "int4_awq", "w4a8_awq", "nvfp4", "nvfp4_awq"]
    ) or args.auto_quantize_bits:
        # If any qformat provided is not fp8, assert model is on GPU
        if args.qformat not in ["fp8", "nvfp4"]:
            assert is_model_on_gpu(model), (
                f"Model must be fully loaded onto GPUs for {args.qformat} calibration. "
                "Please make sure the system has enough GPU memory to load the model."
            )

        if "awq" in args.qformat:
            print(
                "\n####\nAWQ calibration could take longer than other calibration methods. "
                "Consider reducing calib_size to reduce calibration time.\n####\n"
            )

        if args.batch_size == 0:
            # TODO: Enable auto-batch size calculation for AutoQuantize
            assert args.auto_quantize_bits is None, (
                "AutoQuantize requires batch_size to be specified, please specify batch_size."
            )
            # Calibration/sparsification will actually take much more memory than regular inference
            # due to intermediate tensors for fake quantization. Setting sample_memory_usage_ratio
            # to 2 to avoid OOM for AWQ/SmoothQuant fake quantization as it will take more memory than inference.
            sample_memory_usage_ratio = 2 if "awq" in args.qformat or "sq" in args.qformat else 1.1
            args.batch_size = get_max_batch_size(
                model, sample_memory_usage_ratio=sample_memory_usage_ratio
            )
            if args.batch_size > args.calib_size:
                args.batch_size = args.calib_size

        print(f"Use calib batch_size {args.batch_size}")

        calib_dataloader = None
        if model_type == "mllama":
            assert processor is not None and isinstance(processor, MllamaImageProcessor), (
                "The MllamaImageProcessor must be set."
            )
            calib_dataloader = get_vlm_dataset_dataloader(
                dataset_name=args.dataset,
                processor=processor,
                batch_size=args.batch_size,
                num_samples=args.calib_size,
            )
        else:
            assert tokenizer is not None and isinstance(
                tokenizer, (PreTrainedTokenizer, PreTrainedTokenizerFast)
            ), "The PreTrainedTokenizer must be set"
            calib_dataloader = get_dataset_dataloader(
                dataset_name=args.dataset,
                tokenizer=tokenizer,
                batch_size=args.batch_size,
                num_samples=args.calib_size,
                device=device,
                include_labels=args.auto_quantize_bits is not None,
            )

        quant_cfg = None
        if not args.auto_quantize_bits:
            if args.qformat in QUANT_CFG_CHOICES:
                quant_cfg = getattr(mtq, QUANT_CFG_CHOICES[args.qformat])
            else:
                raise ValueError(f"Unsupported quantization format: {args.qformat}")

            if "awq" in args.qformat:
                quant_cfg = copy.deepcopy(getattr(mtq, QUANT_CFG_CHOICES[args.qformat]))
                weight_quantizer = quant_cfg["quant_cfg"]["*weight_quantizer"]
                if isinstance(weight_quantizer, list):
                    weight_quantizer = weight_quantizer[0]
                # If awq_block_size argument is provided, update weight_quantizer
                if args.awq_block_size:
                    weight_quantizer["block_sizes"][-1] = args.awq_block_size

                # Coarser optimal scale search seems to resolve the overflow in TRT-LLM for some models
                if "w4a8_awq" == args.qformat and model_type in ["gemma", "mpt"]:
                    quant_cfg["algorithm"] = {"method": "awq_lite", "alpha_step": 1}

            # Always turn on FP8 kv cache to save memory footprint.
            # For int8_sq, we do not quantize kv cache to preserve accuracy.
            # We turn off FP8 kv cache for unified_hf checkpoint
            enable_quant_kv_cache = (
                "int8_sq" not in args.qformat and not args.disable_kv_cache_quant
            )

            print(f"{'Enable' if enable_quant_kv_cache else 'Disable'} KV cache quantization")
            quant_cfg["quant_cfg"]["*output_quantizer"] = {
                "num_bits": 8 if args.qformat == "int8_sq" else (4, 3),
                "axis": None,
                "enable": enable_quant_kv_cache,
            }

            # Gemma 7B has accuracy regression using alpha 1. We set 0.5 instead.
            if model_type == "gemma" and "int8_sq" in args.qformat:
                quant_cfg["algorithm"] = {"method": "smoothquant", "alpha": 0.5}

        # Only run single sample for preview
        input_ids = next(iter(calib_dataloader))["input_ids"][0:1]
        generated_ids_before_ptq = model.generate(input_ids, max_new_tokens=100)

        model = quantize_model(model, quant_cfg, args, calib_dataloader)
        # Lets print the quantization summary
        mtq.print_quant_summary(model)

        # Run some samples
        generated_ids_after_ptq = model.generate(input_ids, max_new_tokens=100)

        def input_decode(input_ids):
            if processor is not None and isinstance(processor, MllamaImageProcessor):
                return processor.tokenizer.batch_decode(input_ids)
            elif tokenizer is not None:
                return tokenizer.batch_decode(input_ids)
            else:
                raise ValueError("The processor or tokenizer must be set")

        def output_decode(generated_ids, input_shape):
            if tokenizer is not None and is_enc_dec(model_type):
                return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)
            elif processor is not None and isinstance(processor, MllamaImageProcessor):
                return processor.tokenizer.batch_decode(generated_ids[:, input_shape:])
            elif tokenizer is not None:
                return tokenizer.batch_decode(generated_ids[:, input_shape:])
            else:
                raise ValueError("The processor or tokenizer must be set")

        print("--------")
        print(f"example test input: {input_decode(input_ids)}")
        print("--------")
        print(
            f"example outputs before ptq: {output_decode(generated_ids_before_ptq, input_ids.shape[1])}"
        )
        print("--------")
        print(
            f"example outputs after ptq: {output_decode(generated_ids_after_ptq, input_ids.shape[1])}"
        )

    else:
        assert model_type != "dbrx", f"Does not support export {model_type} without quantizaton"
        print(f"No quantization applied, export {device} model")

    with torch.inference_mode():
        if model_type is None:
            print(f"Unknown model type {type(model).__name__}. Continue exporting...")
            model_type = f"unknown:{type(model).__name__}"

        export_path = args.export_path

        if model_type == "mllama":
            full_model_config = model.config
            model = model.language_model
            # TRT-LLM expects both the vision_config and text_config to be set for export.
            setattr(model.config, "vision_config", full_model_config.vision_config)
            setattr(model.config, "text_config", full_model_config.text_config)
            setattr(model.config, "architectures", full_model_config.architectures)

        start_time = time.time()
        if args.export_fmt == "tensorrt_llm":
            # Move meta tensor back to device before exporting.
            remove_hook_from_module(model, recurse=True)

            dtype = None
            if "w4a8_awq" in args.qformat:
                # TensorRT-LLM w4a8 only support fp16 as the dtype.
                dtype = torch.float16

            export_tensorrt_llm_checkpoint(
                model,
                model_type,
                dtype=dtype,
                export_dir=export_path,
                inference_tensor_parallel=args.inference_tensor_parallel,
                inference_pipeline_parallel=args.inference_pipeline_parallel,
            )
        elif args.export_fmt == "hf":
            export_hf_checkpoint(
                model,
                export_dir=export_path,
            )
        else:
            raise NotImplementedError(f"{args.export_fmt} not supported")

        # Restore default padding and export the tokenizer as well.
        if tokenizer is not None:
            tokenizer.padding_side = default_padding_side
            tokenizer.save_pretrained(export_path)

        end_time = time.time()
        print(
            f"Quantized model exported to :{export_path}. Total time used {end_time - start_time}s"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pyt_ckpt_path",
        help="Specify where the PyTorch checkpoint path is",
        required=True,
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--qformat",
        help=(
            "Quantization format. If --auto_quantize_bits is set, this argument specifies the quantization "
            "format for optimal per-layer AutoQuantize search."
        ),
        default="fp8",
    )
    parser.add_argument(
        "--batch_size",
        help="Batch size for calibration. Default to 0 as we calculate max batch size on-the-fly",
        type=int,
        default=0,
    )
    parser.add_argument(
        "--calib_size", help="Number of samples for calibration.", type=int, default=512
    )
    parser.add_argument("--export_path", default="exported_model")
    parser.add_argument("--dataset", help="name of dataset.", type=str, default=None)
    parser.add_argument("--inference_tensor_parallel", type=int, default=1)
    parser.add_argument("--inference_pipeline_parallel", type=int, default=1)
    parser.add_argument("--awq_block_size", default=0, type=int)
    parser.add_argument(
        "--sparsity_fmt",
        help="Sparsity format.",
        default="dense",
        choices=["dense", "sparsegpt"],
    )
    parser.add_argument(
        "--auto_quantize_bits",
        default=None,
        type=float,
        help=(
            "Effective bits constraint for AutoQuantize. If not set, "
            "regular quantization without AutoQuantize search will be applied."
        ),
    )
    parser.add_argument(
        "--disable_kv_cache_quant",
        type=lambda x: x.lower() == "true",
        choices=[True, False],
        help="Disable KV cache quantization (True/False)",
    )
    parser.add_argument(
        "--vlm",
        help="Specify whether this is a visual-language model",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--export_fmt",
        required=False,
        default="tensorrt_llm",
        choices=["tensorrt_llm", "hf"],
        help=("Checkpoint export format"),
    )
    parser.add_argument(
        "--trust_remote_code",
        help="Set trust_remote_code for Huggingface models and tokenizers",
        default=False,
        action="store_true",
    )
    args = parser.parse_args()

    main(args)
