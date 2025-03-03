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

"""Module implementing ``mcore_gpt_minitron`` pruning algorithm for NVIDIA Megatron-Core / NeMo models.

Minitron pruning algorithm uses activation magnitudes to estimate importance of neurons / attention heads in the model.
More details on Minitron pruning algorithm can be found here: https://arxiv.org/pdf/2407.14679
"""

from typing import Optional

import torch

from modelopt.torch.nas.utils import sort_parameters
from modelopt.torch.opt.searcher import BaseSearcher, SearchConfig, SearchStateDict
from modelopt.torch.opt.utils import named_hparams

SUPPORTED_MODEL_CONFIG_MAP = {}  # map from model type to config attribute name
SUPPORTED_HPARAMS = {
    # Width pruning
    "ffn_hidden_size",
    "num_attention_heads",
    "num_query_groups",
    "hidden_size",
    # Depth pruning
    "num_layers",
}

try:
    from megatron.core.models.gpt import GPTModel

    SUPPORTED_MODEL_CONFIG_MAP[GPTModel] = "config"
except Exception:
    pass

try:
    from nemo.collections import llm
    from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel

    SUPPORTED_MODEL_CONFIG_MAP[MegatronGPTModel] = "cfg"
    SUPPORTED_MODEL_CONFIG_MAP[llm.GPTModel] = "config"
except Exception:
    pass


class MCoreGPTMinitronSearcher(BaseSearcher):
    """Searcher for Minitron pruning algorithm."""

    @property
    def default_search_config(self) -> SearchConfig:
        """Get the default config for the searcher."""
        return {**super().default_search_config, "max_iter_data_loader": 1024}

    @property
    def default_state_dict(self) -> SearchStateDict:
        """Return default state dict."""
        return {}  # Not used

    def sanitize_search_config(self, config: Optional[SearchConfig]) -> SearchConfig:
        """Sanitize the search config dict."""
        config = super().sanitize_search_config(config)
        assert config["data_loader"] or config["forward_loop"], (
            "Data loader or forward loop must be provided for importance estimation!"
        )
        return config

    def before_search(self) -> None:
        """Optional pre-processing steps before the search."""
        super().before_search()

        # Check that the constraint is valid
        assert self.constraints.keys() == {"export_config"}, (
            "Only `export_config` constraint is supported for pruning!"
        )

        export_config = self.constraints["export_config"]
        assert isinstance(export_config, dict)  # to keep mypy happy
        assert export_config.keys() <= SUPPORTED_HPARAMS, (
            f"Only {SUPPORTED_HPARAMS} are supported for pruning!"
        )

        assert ("num_attention_heads" in export_config and "num_query_groups" in export_config) or (
            "num_attention_heads" not in export_config and "num_query_groups" not in export_config
        ), "Both `num_attention_heads` and `num_query_groups` should be provided together!"

        # Only sort the parameters that are to be pruned
        # If a user only prunes depth, we should not sort width parameters
        self.hps_to_sort = SUPPORTED_HPARAMS & export_config.keys()

        # Convert `num_attention_heads` to `num_heads_per_group`
        # Still keep `num_attention_heads` for updating model_cfg below
        if "num_attention_heads" in export_config and "num_query_groups" in export_config:
            export_config["num_heads_per_group"] = (
                export_config["num_attention_heads"] // export_config["num_query_groups"]
            )
            self.hps_to_sort.add("num_heads_per_group")

        for n, hp in named_hparams(self.model, configurable=True):
            hp_name = n.split(".")[-1]
            if hp_name in export_config:
                assert export_config[hp_name] in hp.choices, (
                    f"Invalid choice for {hp_name}! Available choices: {hp.choices}"
                )

    def run_search(self) -> None:
        """Run actual search."""
        # Run forward loop to collect activations and sort parameters
        assert self.forward_loop is not None
        is_training = self.model.training
        self.model.eval()
        with torch.no_grad():
            self.forward_loop(self.model)
        sort_parameters(self.model, self.hps_to_sort)
        self.model.train(is_training)

        # Prune homogeneously
        export_config = self.constraints["export_config"]
        assert isinstance(export_config, dict)  # to keep mypy happy
        for n, hp in named_hparams(self.model, configurable=True):
            hp_name = n.split(".")[-1]
            if hp_name in export_config:
                hp.active = export_config[hp_name]

        model_cfg = None
        for m_type, cfg_name in SUPPORTED_MODEL_CONFIG_MAP.items():
            if isinstance(self.model, m_type):
                model_cfg = getattr(self.model, cfg_name)
                break
        if model_cfg is None:
            raise NotImplementedError(
                f"Only {SUPPORTED_MODEL_CONFIG_MAP.keys()} models are supported! Got: {type(self.model)}"
            )

        # kv_channels can be None so we need to save original from original hidden_size and num_attention_heads
        orig_kv_channels = getattr(model_cfg, "kv_channels")
        if orig_kv_channels is None:
            orig_kv_channels = getattr(model_cfg, "hidden_size") // getattr(
                model_cfg, "num_attention_heads"
            )
        setattr(model_cfg, "kv_channels", orig_kv_channels)
        for n in SUPPORTED_HPARAMS:
            if n in export_config:
                setattr(model_cfg, n, export_config[n])
