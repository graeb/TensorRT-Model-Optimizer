===================================
Model Optimizer Changelog (Windows)
===================================

0.19 (2024-11-18)
^^^^^^^^^^^^^^^^^

**New Features**

- This is the first official release of TensorRT Model Optimizer for Windows
- **ONNX INT4 Quantization:** :meth:`modelopt.onnx.quantization.quantize_int4 <modelopt.onnx.quantization.int4.quantize>` now supports ONNX INT4 quantization for DirectML and TensorRT* deployment. See :ref:`Support_Matrix` for details about supported features and models.
- **LLM Quantization with Olive:** Enabled LLM quantization through Olive, streamlining model optimization workflows. Refer `example <https://github.com/microsoft/Olive/tree/main/examples/phi3#quantize-models-with-nvidia-tensorrt-model-optimizer>`_
- **DirectML Deployment Guide:** Added DML deployment guide. Refer :ref:`DirectML_Deployment`.
- **MMLU Benchmark for Accuracy Evaluations:** Introduced `MMLU benchmarking <https://github.com/NVIDIA/TensorRT-Model-Optimizer/tree/main/examples/windows/accuracy_benchmark/README.md>`_ for accuracy evaluation of ONNX models on DirectML (DML).
- **Published quantized ONNX models collection:** Published quantized ONNX models at HuggingFace `NVIDIA collections <https://huggingface.co/collections/nvidia/optimized-onnx-models-for-nvidia-rtx-gpus-67373fe7c006ebc1df310613>`_.


\* *This version includes experimental features such as TensorRT deployment of ONNX INT4 models, PyTorch quantization and sparsity. These are currently unverified on Windows.*
