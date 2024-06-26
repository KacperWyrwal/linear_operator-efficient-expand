#!/usr/bin/env python3
from __future__ import annotations

import itertools
from typing import List, Optional, Tuple, Union

import torch
from jaxtyping import Float
from torch import Tensor

from linear_operator import settings
from linear_operator.operators._linear_operator import IndexType, LinearOperator
from linear_operator.utils.broadcasting import _matmul_broadcast_shape
from linear_operator.utils.memoize import cached


class BatchExpandLinearOperator(LinearOperator):
    def __init__(self, base_linear_op, batch_repeat=torch.Size((-1,))):
        if settings.debug.on():
            if not isinstance(batch_repeat, torch.Size):
                raise RuntimeError(
                    "batch_repeat must be a torch.Size, got a {} instead".format(batch_repeat.__class__.__name__)
                )
            if isinstance(base_linear_op, BatchExpandLinearOperator):
                raise RuntimeError(
                    "BatchRepeatLinearOperator received the following args:\n"
                    "base_linear_op: {} (size: {}), batch_repeat: {}.".format(
                        base_linear_op, base_linear_op.shape, batch_repeat
                    )
                )

        # Are we adding batch dimensions to the lazy tensor?
        # If so, we'll unsqueeze the base_linear_op so it has the same number of dimensions
        for _ in range(len(batch_repeat) + 2 - base_linear_op.dim()):
            base_linear_op = base_linear_op.unsqueeze(0)

        super().__init__(base_linear_op, batch_repeat=batch_repeat)
        self.base_linear_op = base_linear_op
        self.batch_repeat = batch_repeat

    @cached(name="cholesky")
    def _cholesky(
        self: Float[LinearOperator, "*batch N N"], upper: Optional[bool] = False
    ) -> Float[LinearOperator, "*batch N N"]:
        from linear_operator.operators.triangular_linear_operator import TriangularLinearOperator

        res = self.base_linear_op.cholesky(upper=upper)._tensor
        res = res.expand(*self.batch_repeat, -1, -1)
        return TriangularLinearOperator(res, upper=upper)

    def _cholesky_solve(
        self: Float[LinearOperator, "*batch N N"],
        rhs: Union[Float[LinearOperator, "*batch2 N M"], Float[Tensor, "*batch2 N M"]],
        upper: Optional[bool] = False,
    ) -> Union[Float[LinearOperator, "... N M"], Float[Tensor, "... N M"]]:
        # TODO: Figure out how to deal with this with TriangularLinearOperator if returned by _cholesky
        output_shape = _matmul_broadcast_shape(self.shape, rhs.shape)
        if rhs.shape != output_shape:
            rhs = rhs.expand(*output_shape)

        rhs = self._move_repeat_batches_to_columns(rhs, output_shape)
        res = self.base_linear_op._cholesky_solve(rhs, upper=upper)
        res = self._move_repeat_batches_back(res, output_shape)
        return res

    def _compute_batch_repeat_size(
        self, current_batch_shape: Union[torch.Size, List[int]], desired_batch_shape: Union[torch.Size, List[int]]
    ) -> torch.Size:
        # batch_repeat = torch.Size(
        #     desired_batch_size // current_batch_size
        #     for desired_batch_size, current_batch_size in zip(desired_batch_shape, current_batch_shape)
        # )
        batch_repeat = torch.Size(
            desired_batch_shape
            for desired_batch_shape, current_batch_shape in zip(desired_batch_shape, current_batch_shape)
        )
        return batch_repeat

    def _expand_batch(
        self: Float[LinearOperator, "... M N"], batch_shape: Union[torch.Size, List[int]]
    ) -> Float[LinearOperator, "... M N"]:
        padding_dims = torch.Size(tuple(-1 for _ in range(max(len(batch_shape) + 2 - self.base_linear_op.dim(), 0))))
        current_batch_shape = padding_dims + self.base_linear_op.batch_shape
        return self.__class__(
            self.base_linear_op, batch_repeat=self._compute_batch_repeat_size(current_batch_shape, batch_shape)
        )

    def _get_indices(self, row_index: IndexType, col_index: IndexType, *batch_indices: IndexType) -> torch.Tensor:
        # First remove any new batch indices that were added - they aren't necessary
        num_true_batch_indices = self.base_linear_op.dim() - 2
        batch_indices = batch_indices[len(batch_indices) - num_true_batch_indices :]

        # Now adjust the indices batch_indices that were repeated
        batch_indices = [
            batch_index.fmod(size) for batch_index, size in zip(batch_indices, self.base_linear_op.batch_shape)
        ]

        # Now call the sub _get_indices method
        res = self.base_linear_op._get_indices(row_index, col_index, *batch_indices)
        return res

    def _getitem(self, row_index: IndexType, col_index: IndexType, *batch_indices: IndexType) -> LinearOperator:
        args = []
        kwargs = self.base_linear_op._kwargs
        num_base_batch_dims = len(self.base_linear_op.batch_shape)

        for arg in self.base_linear_op._args:
            if torch.is_tensor(arg) or isinstance(arg, LinearOperator):
                arg_base_shape_len = max(arg.dim() - num_base_batch_dims, 0)
                args.append(arg.expand(*self.batch_repeat, *[-1 for _ in range(arg_base_shape_len)]))
            else:
                args.append(arg)

        new_linear_op = self.base_linear_op.__class__(*args, **kwargs)
        return new_linear_op._getitem(row_index, col_index, *batch_indices)

    def _matmul(
        self: Float[LinearOperator, "*batch M N"],
        rhs: Union[Float[torch.Tensor, "*batch2 N C"], Float[torch.Tensor, "*batch2 N"]],
    ) -> Union[Float[torch.Tensor, "... M C"], Float[torch.Tensor, "... M"]]:
        output_shape = _matmul_broadcast_shape(self.shape, rhs.shape)

        # # only attempt broadcasting if the non-batch dimensions are the same
        # if self.is_square:
        #     if rhs.shape != output_shape:
        #         rhs = rhs.expand(*output_shape)

        #     rhs = self._move_repeat_batches_to_columns(rhs, output_shape)
        #     res = self.base_linear_op._matmul(rhs)
        #     res = self._move_repeat_batches_back(res, output_shape)
        #     return res
        # else:
        #     # otherwise, we will rely on base tensor broadcasting
        res = self.base_linear_op._matmul(rhs)
        if res.shape != output_shape:
            res = res.expand(*output_shape)

        return res

    def _move_repeat_batches_back(self, batch_matrix, output_shape):
        """
        The opposite of _move_repeat_batches_to_columns

        Takes a b x m x nr tensor, and moves the batches associated with repeating
        So that the tensor is now rb x m x n.
        """
        if hasattr(self, "_batch_move_memo"):
            padded_base_batch_shape, batch_repeat = self.__batch_move_memo
            del self.__batch_move_memo
        else:
            padding_dims = torch.Size(tuple(-1 for _ in range(max(len(output_shape) - self.base_linear_op.dim(), 0))))
            padded_base_batch_shape = padding_dims + self.base_linear_op.batch_shape
            batch_repeat = self._compute_batch_repeat_size(padded_base_batch_shape, output_shape[:-2])

        # Now we have to move the columns back to their original repeat dimensions
        batch_matrix = batch_matrix.view(*padded_base_batch_shape, output_shape[-2], -1, *batch_repeat)
        output_dims = len(output_shape)
        dims = tuple(
            itertools.chain.from_iterable([i + output_dims, i] for i in range(len(padded_base_batch_shape)))
        ) + (output_dims - 2, output_dims - 1)
        batch_matrix = batch_matrix.permute(*dims).contiguous()

        # Combine the repeat and the batch dimensions, and return the batch_matrix
        batch_matrix = batch_matrix.view(*output_shape)
        return batch_matrix

    def _move_repeat_batches_to_columns(self, batch_matrix, output_shape):
        """
        Takes a rb x m x n tensor, and moves the batches associated with repeating
        So that the tensor is now b x m x nr.
        This allows us to use the base_linear_op routines.
        """
        padding_dims = torch.Size(tuple(-1 for _ in range(max(len(output_shape) - self.base_linear_op.dim(), 0))))
        padded_base_batch_shape = padding_dims + self.base_linear_op.batch_shape
        batch_repeat = self._compute_batch_repeat_size(padded_base_batch_shape, output_shape[:-2])

        # Reshape batch_matrix so that each batch dimension is split in two:
        # The repeated part, and the actual part
        split_shape = torch.Size(
            tuple(
                itertools.chain.from_iterable(
                    [repeat, size] for repeat, size in zip(batch_repeat, padded_base_batch_shape)
                )
            )
            + output_shape[-2:]
        )
        batch_matrix = batch_matrix.view(*split_shape)

        # Now chuck the repeat parts of the batch dimensions into the last dimension of batch_matrix
        # These will act like extra columns of the batch matrix that we are multiplying against
        # The repeated part, and the actual part
        repeat_dims = range(0, len(batch_repeat) * 2, 2)
        batch_dims = range(1, len(batch_repeat) * 2, 2)
        batch_matrix = batch_matrix.permute(*batch_dims, -2, -1, *repeat_dims).contiguous()
        batch_matrix = batch_matrix.view(*self.base_linear_op.batch_shape, output_shape[-2], -1)

        self.__batch_move_memo = output_shape, padded_base_batch_shape, batch_repeat
        return batch_matrix

    def _permute_batch(self, *dims: int) -> LinearOperator:
        new_batch_repeat = torch.Size(tuple(self.batch_repeat[dim] for dim in dims))
        res = self.__class__(self.base_linear_op._permute_batch(*dims), batch_repeat=new_batch_repeat)
        return res

    def _bilinear_derivative(self, left_vecs: Tensor, right_vecs: Tensor) -> Tuple[Optional[Tensor], ...]:
        if self.is_square:
            left_output_shape = _matmul_broadcast_shape(self.shape, left_vecs.shape)
            if left_output_shape != left_vecs.shape:
                left_vecs = left_vecs.expand(left_output_shape)

            right_output_shape = _matmul_broadcast_shape(self.shape, right_vecs.shape)
            if right_output_shape != right_vecs.shape:
                right_vecs = right_vecs.expand(right_output_shape)

            left_vecs = self._move_repeat_batches_to_columns(left_vecs, left_output_shape)
            right_vecs = self._move_repeat_batches_to_columns(right_vecs, right_output_shape)

            return self.base_linear_op._bilinear_derivative(left_vecs, right_vecs)
        else:
            return super()._bilinear_derivative(left_vecs, right_vecs)

    def _root_decomposition(
        self: Float[LinearOperator, "... N N"]
    ) -> Union[Float[torch.Tensor, "... N N"], Float[LinearOperator, "... N N"]]:
        return self.base_linear_op._root_decomposition().expand(*self.batch_repeat, -1, -1)

    def _root_inv_decomposition(
        self: Float[LinearOperator, "*batch N N"],
        initial_vectors: Optional[torch.Tensor] = None,
        test_vectors: Optional[torch.Tensor] = None,
    ) -> Union[Float[LinearOperator, "... N N"], Float[Tensor, "... N N"]]:
        return self.base_linear_op._root_inv_decomposition().expand(*self.batch_repeat, -1, -1)

    def _size(self) -> torch.Size:
        return self.batch_repeat + self.base_linear_op.shape[-2:]
        repeated_batch_shape = torch.Size(
            size * repeat for size, repeat in zip(self.base_linear_op.batch_shape, self.batch_repeat)
        )
        res = torch.Size(repeated_batch_shape + self.base_linear_op.matrix_shape)
        return res

    def _transpose_nonbatch(self: Float[LinearOperator, "*batch M N"]) -> Float[LinearOperator, "*batch N M"]:
        return self.__class__(self.base_linear_op._transpose_nonbatch(), batch_repeat=self.batch_repeat)

    def _unsqueeze_batch(self, dim: int) -> LinearOperator:
        base_linear_op = self.base_linear_op
        batch_repeat = list(self.batch_repeat)
        batch_repeat.insert(dim, 1)
        batch_repeat = torch.Size(batch_repeat)
        # If the dim only adds a new padded dimension, then we're done
        # Otherwise we have to also unsqueeze the base_linear_op
        base_unsqueeze_dim = dim - (len(self.base_linear_op.batch_shape) - len(self.base_linear_op.batch_shape))
        if base_unsqueeze_dim > 0:
            base_linear_op = base_linear_op._unsqueeze_batch(base_unsqueeze_dim)
        return self.__class__(base_linear_op, batch_repeat=batch_repeat)

    def add_jitter(
        self: Float[LinearOperator, "*batch N N"], jitter_val: float = 1e-3
    ) -> Float[LinearOperator, "*batch N N"]:
        return self.__class__(self.base_linear_op.add_jitter(jitter_val=jitter_val), batch_repeat=self.batch_repeat)

    def inv_quad_logdet(
        self: Float[LinearOperator, "*batch N N"],
        inv_quad_rhs: Optional[Union[Float[Tensor, "*batch N M"], Float[Tensor, "*batch N"]]] = None,
        logdet: Optional[bool] = False,
        reduce_inv_quad: Optional[bool] = True,
    ) -> Tuple[
        Optional[Union[Float[Tensor, "*batch M"], Float[Tensor, " *batch"], Float[Tensor, " 0"]]],
        Optional[Float[Tensor, "..."]],
    ]:
        if not self.is_square:
            raise RuntimeError(
                "inv_quad_logdet only operates on (batches of) square (positive semi-definite) LinearOperators. "
                "Got a {} of size {}.".format(self.__class__.__name__, self.size())
            )

        if inv_quad_rhs is not None:
            if self.dim() != inv_quad_rhs.dim():
                raise RuntimeError(
                    "LinearOperator (size={}) and right-hand-side Tensor (size={}) should have the same number "
                    "of dimensions.".format(self.shape, inv_quad_rhs.shape)
                )
            elif self.batch_shape != inv_quad_rhs.shape[:-2] or self.shape[-1] != inv_quad_rhs.shape[-2]:
                raise RuntimeError(
                    "LinearOperator (size={}) cannot be multiplied with right-hand-side Tensor (size={}).".format(
                        self.shape, inv_quad_rhs.shape
                    )
                )

        if inv_quad_rhs is not None:
            output_shape = _matmul_broadcast_shape(self.shape, inv_quad_rhs.shape)
            inv_quad_rhs = self._move_repeat_batches_to_columns(inv_quad_rhs, output_shape)

        inv_quad_term, logdet_term = self.base_linear_op.inv_quad_logdet(inv_quad_rhs, logdet, reduce_inv_quad=False)

        if inv_quad_term is not None and inv_quad_term.numel():
            inv_quad_term = inv_quad_term.view(*inv_quad_term.shape[:-1], -1, 1, self.batch_repeat.numel())
            output_shape = list(output_shape)
            output_shape[-2] = 1
            inv_quad_term = self._move_repeat_batches_back(inv_quad_term, output_shape).squeeze(-2)
            if reduce_inv_quad:
                inv_quad_term = inv_quad_term.sum(-1)

        if logdet_term is not None and logdet_term.numel():
            logdet_term = logdet_term.expand(*self.batch_repeat)

        return inv_quad_term, logdet_term

    def repeat(self, *sizes: Union[int, Tuple[int, ...]]) -> LinearOperator:
        if len(sizes) < 3 or tuple(sizes[-2:]) != (1, 1):
            raise RuntimeError(
                "Invalid repeat arguments {}. Currently, repeat only works to create repeated "
                "batches of a 2D LinearOperator.".format(tuple(sizes))
            )

        padded_batch_repeat = tuple(1 for _ in range(len(sizes) - 2 - len(self.batch_repeat))) + self.batch_repeat
        return self.__class__(
            self.base_linear_op,
            batch_repeat=torch.Size(
                orig_repeat_size * new_repeat_size
                for orig_repeat_size, new_repeat_size in zip(padded_batch_repeat, sizes[:-2])
            ),
        )
    
    def to_dense(self) -> Float[Tensor, "*batch M N"]:
        return self.base_linear_op.to_dense().expand(*self.batch_repeat, -1, -1)
    
    def _diagonal(self) -> Float[Tensor, "... N"]:
        return self.base_linear_op._diagonal().expand(*self.batch_repeat, -1)

    @cached(name="svd")
    def _svd(
        self: Float[LinearOperator, "*batch N N"]
    ) -> Tuple[Float[LinearOperator, "*batch N N"], Float[Tensor, "... N"], Float[LinearOperator, "*batch N N"]]:
        U_, S_, V_ = self.base_linear_op.svd()
        U = U_.expand(*self.batch_repeat, -1, -1)
        S = S_.expand(*self.batch_repeat, -1)
        V = V_.expand(*self.batch_repeat, -1, -1)
        return U, S, V

    def _symeig(
        self: Float[LinearOperator, "*batch N N"],
        eigenvectors: bool = False,
        return_evals_as_lazy: Optional[bool] = False,
    ) -> Tuple[Float[Tensor, "*batch M"], Optional[Float[LinearOperator, "*batch N M"]]]:
        evals, evecs = self.base_linear_op._symeig(eigenvectors=eigenvectors)
        evals = evals.expand(*self.batch_repeat, -1)
        if eigenvectors:
            evecs = evecs.expand(*self.batch_repeat, -1, -1)
        return evals, evecs
    
    def __add__(self, other: Float[LinearOperator, "*batch N N"]) -> Float[LinearOperator, "*batch N N"]:
        return BatchExpandLinearOperator(self.base_linear_op + other, batch_repeat=self.batch_repeat)
