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

from functools import partial

from _test_utils.import_helper import skip_if_mcore_dist_ckpt_is_not_supported, skip_if_no_megatron
from _test_utils.torch_dist.dist_utils import spawn_multiprocess_job
from _test_utils.torch_model.utils import sample_subnet_with_sparsity

from modelopt.torch.opt.conversion import apply_mode

skip_if_no_megatron()

from _test_utils.torch_dist.plugins.megatron_common import (
    MegatronModel,
    initialize_for_megatron,
    sharded_state_dict_test_helper,
)


def _test_sharded_state_dict(tmpdir, rank, size):
    initialize_for_megatron()

    model_ref = MegatronModel(size).cuda()
    input = model_ref.get_dummy_input().cuda()

    model_ref = apply_mode(model_ref, "sparse_magnitude")
    sample_subnet_with_sparsity(model_ref)

    model_test = MegatronModel(size).cuda()

    sharded_state_dict_test_helper(tmpdir, model_ref, model_test, lambda model: model(input))


def test_sharded_state_dict(tmpdir):
    skip_if_mcore_dist_ckpt_is_not_supported()
    spawn_multiprocess_job(size=1, job=partial(_test_sharded_state_dict, tmpdir), backend="nccl")
