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

"""Quantization utilities."""

from contextlib import ExitStack, contextmanager

import torch
import torch.nn.functional as F

from modelopt.torch.utils.distributed import ParallelState

__all__ = [
    "reduce_amax",
    "is_quantized",
    "is_quantized_layer_with_weight",
    "is_quantized_column_parallel_linear",
    "is_quantized_row_parallel_linear",
    "replace_function",
    "EXPORT_MODE",
    "export_torch_mode",
    "get_parallel_state",
]


def reduce_block_amax(input_tensor: torch.Tensor, block_sizes: dict):
    """Computes the amax of the input tensor using block-based reduction for each dimension.

    Args:
        input_tensor (torch.Tensor): The input tensor.
        block_sizes (dict): A dictionary specifying the block size for each dimension.
                            Example: `{-1: 128, -2: 128}` reduces over 2D blocks.

    Returns:
        torch.Tensor: The reduced tensor with amax computed per block.

    Example:
        Input Shape: [256, 512]
        Block Sizes: {-1: 128, -2: 128}
        Process:
            - Block along last dim → Shape [256, 4, 128]
            - Compute block-wise amax → Shape [256, 4]
            - Block along second-to-last dim → Shape [2, 128, 4]
            - Compute block-wise amax → Shape [2, 4]
    """
    with torch.no_grad():
        amax = input_tensor.clone()

        for dim, block_size in block_sizes.items():
            # Convert negative dimensions to positive
            dim = dim if dim >= 0 else len(amax.shape) + dim
            assert amax.shape[dim] % block_size == 0, (
                f"Tensor dimension {amax.shape[dim]}, {amax.shape[dim]} is not divisible by {block_size}"
            )

            # Compute new shape for blocking
            outer_dim = amax.shape[dim] // block_size
            new_shape = (
                list(amax.shape[:dim]) + [outer_dim, block_size] + list(amax.shape[dim + 1 :])
            )

            # Reshape into blocks
            amax = amax.reshape(new_shape)

            # Reduce along the newly created block dimension
            # Shift by 1 because we added an extra dimension
            amax = reduce_amax(amax, dim + 1, keepdims=False, squeeze_scalar=False)

        return amax


def reduce_block_padding(input: torch.Tensor, block_sizes: dict, pad_value: float = 0):
    """Padding the input using block-based reduction for each dimension.

    Args:
        input_tensor (torch.Tensor): The input tensor.
        block_sizes (dict): A dictionary specifying the block size for padding each dimension.
                            Example: `{-1: 128, -2: 128}` pads the input over 2D blocks.
    """
    with torch.no_grad():
        padded_tensor = input
        num_dims = padded_tensor.dim()

        # Process each specified dimension independently
        for dim, block in block_sizes.items():
            # Convert negative dimension to positive index
            pos_dim = dim if dim >= 0 else num_dims + dim

            # Calculate how many elements are missing along that dimension
            current_size = padded_tensor.size(pos_dim)
            remainder = current_size % block
            pad_amt = 0 if remainder == 0 else block - remainder

            if pad_amt > 0:
                # F.pad expects a pad tuple of length 2*num_dims.
                pad = [0] * (2 * num_dims)
                # For dimension pos_dim, the right padding is at index: (num_dims - 1 - pos_dim)*2 + 1.
                pad_index = (num_dims - 1 - pos_dim) * 2
                pad[pad_index + 1] = (
                    pad_amt  # Set padding on the right side of the target dimension
                )

                padded_tensor = F.pad(padded_tensor, pad, value=pad_value)

        return padded_tensor


def reduce_amax(input, axis=None, keepdims=True, squeeze_scalar=True):
    """Compute the absolute maximum value of a tensor.

    Reduces input_tensor along the dimensions given in axis. Unless keepdims is true,
    the rank of the tensor is reduced by 1 for each entry in axis. If keepdims is true,
    the reduced dimensions are retained with length 1.

    .. note::
        Gradient computation is disabled as this function is never meant learning reduces amax

    Args:
        input: Input tensor
        axis: The dimensions to reduce. None or int or tuple of ints. If None (the default),
            reduces all dimensions. Must be in the range [-rank(input_tensor), rank(input_tensor)).
        keepdims: A boolean. If true, retains reduced dimensions with length 1. Default True
        granularity: DEPRECTED. specifies if the statistic has to be calculated at tensor or channel granularity

    Returns:
        The reduced tensor.

    Raises:
        ValueError: Any axis which doesn't make sense or is not supported
        ValueError: If unknown granularity is passed in.
    """
    with torch.no_grad():
        # A memory-efficient implementation that avoids copying input tensor
        if axis is None:
            max_val = torch.max(input)
            min_val = torch.min(input)
            output = torch.maximum(torch.abs(max_val), torch.abs(min_val))
        else:
            if isinstance(axis, int):
                axis = (axis,)
            max_val = torch.amax(input, dim=axis, keepdim=keepdims)
            min_val = torch.amin(input, dim=axis, keepdim=keepdims)
            output = torch.maximum(torch.abs(max_val), torch.abs(min_val))
            if squeeze_scalar and output.numel() == 1:
                output.squeeze_()
        return output


def is_quantized(module):
    """Check if a module is quantized."""
    from .nn import TensorQuantizer

    for _module in module.modules():
        if isinstance(_module, TensorQuantizer):
            return True
    return False


def is_quantized_layer_with_weight(module):
    """Check if a module is quantized with weights."""
    return is_quantized(module) and getattr(module, "weight", None) is not None


def is_quantized_linear(module):
    """Check if a module is a quantized linear module."""
    return (
        hasattr(module, "input_quantizer")
        and hasattr(module, "weight_quantizer")
        and getattr(module, "weight", None) is not None
        and module.weight.dim() == 2
    )


def is_quantized_column_parallel_linear(module):
    """Check if a module is a quantized column parallel linear module."""
    return is_quantized_linear(module) and getattr(module, "_is_column_parallel", False)


def is_quantized_row_parallel_linear(module):
    """Check if a module is a quantized row parallel linear module."""
    return is_quantized_linear(module) and getattr(module, "_is_row_parallel", False)


@contextmanager
def replace_function(package, name, new_func):
    """Replace a function with a new one within a context."""
    old_func = getattr(package, name)
    setattr(package, name, new_func)
    setattr(package, "_" + name, old_func)
    yield
    setattr(package, name, old_func)
    delattr(package, "_" + name)


@contextmanager
def multi_context(*cms):
    """Context manager enabling variable number of context managers."""
    with ExitStack() as stack:
        yield [stack.enter_context(cls) for cls in cms]


EXPORT_MODE: bool = False


@contextmanager
def export_torch_mode():
    """Context manager enabling the export mode."""
    global EXPORT_MODE
    original_value = EXPORT_MODE
    EXPORT_MODE = True
    try:
        yield
    finally:
        EXPORT_MODE = original_value


def is_torch_export_mode():
    """Check whether in the context of exporting model to torch."""
    return EXPORT_MODE


def get_parallel_state(model, name=None) -> ParallelState:
    """Get the parallel state.

    Args:
        model: Pytorch model.
        name: The name of the submodule of the model to get the parallel state from. If None,
            the parallel state of the model is returned.
    """
    if name is None:
        return getattr(model, "_parallel_state", ParallelState())

    # If the submodule does not have a parallel state, get the parallel state of the parent module
    module = model.get_submodule(name)
    if hasattr(module, "_parallel_state"):
        return module._parallel_state
    parent_module = model.get_submodule(name.rpartition(".")[0])
    return getattr(parent_module, "_parallel_state", ParallelState())


def is_pow2(n):
    """Check if a number is the power of 2."""
    return (n != 0) and (n & (n - 1) == 0)
