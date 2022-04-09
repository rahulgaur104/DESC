import numpy as np
from abc import ABC, abstractmethod
from inspect import getfullargspec

from desc import config
from desc.backend import use_jax, jnp, jit, put
from desc.utils import Timer
from desc.io import IOAble
from desc.derivatives import Derivative
from desc.compute import arg_order

# XXX: could use `indicies` instead of `arg_order` in ObjectiveFunction loops


class ObjectiveFunction(IOAble):
    """Objective function comprised of one or more Objectives."""

    _io_attrs_ = ["objectives", "constraints"]

    def __init__(self, objectives, constraints=(), eq=None, use_jit=True, verbose=1):
        """Initialize an Objective Function.

        Parameters
        ----------
        objectives : tuple of Objective
            List of objectives to be targeted during optimization.
        constraints : tuple of Objective, optional
            List of objectives to be used as constraints during optimization.
        eq : Equilibrium, optional
            Equilibrium that will be optimized to satisfy the Objective.
        use_jit : bool, optional
            Whether to just-in-time compile the objective and derivatives.
        verbose : int, optional
            Level of output.

        """
        if not isinstance(objectives, tuple):
            objectives = (objectives,)
        if not isinstance(constraints, tuple):
            constraints = (constraints,)

        self._objectives = objectives
        self._constraints = constraints
        self._use_jit = use_jit
        self._built = False
        self._compiled = False

        if eq is not None:
            self.build(eq, use_jit=self._use_jit, verbose=verbose)

    def _set_state_vector(self):
        """Set state vector components, dimensions, and indicies."""
        self._args = np.concatenate([obj.args for obj in self.objectives])
        if self.constraints:
            self._args = np.concatenate(
                (self.args, np.concatenate([obj.args for obj in self.constraints]))
            )
        self._args = np.unique(self.args)

        self._dimensions = self.objectives[0].dimensions

        self._b_idx = {}
        for arg in arg_order:
            if arg not in self._b_idx:
                self._b_idx[arg] = np.array([], dtype=int)
            idx = 0
            for obj in self.constraints:
                if arg == obj.target_arg:
                    indicies = np.arange(idx, idx + obj.dim_f)
                    self._b_idx[arg] = (
                        np.hstack((self._b_idx[arg], indicies))
                        if self._b_idx[arg].size
                        else indicies
                    )
                idx += obj.dim_f

        idx = 0
        self._y_idx = {}
        for arg in arg_order:
            if arg in self.args:
                self._y_idx[arg] = np.arange(idx, idx + self.dimensions[arg])
                idx += self.dimensions[arg]
            else:
                self._y_idx[arg] = np.array([], dtype=int)

        self._dim_y = idx

    def _build_linear_constraints(self):
        """Compute and factorize A to get pseudoinverse and nullspace."""
        # A*y = b
        self._A = np.array([[]])
        self._b = np.array([])
        for obj in self.constraints:
            A = np.array([[]])
            for arg in arg_order:
                if arg in self.args:
                    a = np.atleast_2d(obj.derivatives[arg])
                    A = np.hstack((A, a)) if A.size else a
            b = obj.target
            self._A = np.vstack((self._A, A)) if self._A.size else A
            self._b = np.hstack((self._b, b)) if self._b.size else b

        # TODO: handle duplicate constraints
        row_idx = []
        col_idx = []
        self._A_full = self._A
        self._b_full = self._b
        dimy = self._A.shape[1]
        for i, row in enumerate(self._A):
            if np.count_nonzero(row) == 1:
                j = np.where(row)[0][0]
                if np.count_nonzero(self._A[:, j]) == 1:
                    row_idx.append(i)
                    col_idx.append(j)
        row_idx = np.array(row_idx)
        col_idx = np.array(col_idx)
        self._fixed_rows = row_idx
        self._fixed_idx = col_idx
        self._unfixed_rows = np.setdiff1d(np.arange(self._A.shape[0]), self._fixed_rows)
        self._unfixed_idx = np.setdiff1d(np.arange(self._A.shape[1]), self._fixed_idx)
        self._fixed_vals = self._b[self._fixed_rows]
        self._A = self._A[self._unfixed_rows][:, self._unfixed_idx]
        self._b = self._b[self._unfixed_rows]

        if self.constraints:
            # SVD of A to get Ainv = pseudoinverse and Z= null space of A
            u, s, vh = np.linalg.svd(self._A, full_matrices=True)
            M, N = u.shape[0], vh.shape[1]
            K = min(M, N)
            rcond = np.finfo(self._A.dtype).eps * max(M, N)
            tol = np.amax(s) * rcond
            large = s > tol
            num = np.sum(large, dtype=int)
            uk = u[:, :K]
            vhk = vh[:K, :]
            s = np.divide(1, s, where=large, out=s)
            s[(~large,)] = 0
            self._Ainv = np.matmul(vhk.T, np.multiply(s[..., np.newaxis], uk.T))
            temp_y0 = np.dot(self._Ainv, self._b)
            self._y0 = jnp.zeros(dimy)
            self._y0 = put(self._y0, self._unfixed_idx, temp_y0)
            self._y0 = put(self._y0, self._fixed_idx, self._fixed_vals)
            self._Z = vh[num:, :].T.conj()
            self._dim_x = self._Z.shape[1]
        else:
            self._Ainv = np.array([[]])
            self._y0 = np.zeros((self._dim_y,))
            self._Z = np.eye(self._dim_y)
            self._dim_x = self._dim_y

    def _set_derivatives(self, use_jit=True):
        """Set up derivatives of the objective functions.

        Parameters
        ----------
        use_jit : bool, optional
            Whether to just-in-time compile the objective and derivatives.

        """
        self._grad = Derivative(self.compute_scalar, mode="grad", use_jit=use_jit)
        self._hess = Derivative(
            self.compute_scalar,
            mode="hess",
            use_jit=use_jit,
        )
        self._jac = Derivative(
            self.compute,
            mode="fwd",
            use_jit=use_jit,
        )

        if use_jit:
            self.compute = jit(self.compute)
            self.compute_scalar = jit(self.compute_scalar)
            # TODO: add jit for jac, hess, jvp, etc.
            # then can remove jit from Derivatives class

    def build(self, eq, use_jit=True, verbose=1):
        """Build the constraints and objectives.

        Parameters
        ----------
        eq : Equilibrium, optional
            Equilibrium that will be optimized to satisfy the Objective.
        use_jit : bool, optional
            Whether to just-in-time compile the objective and derivatives.
        verbose : int, optional
            Level of output.

        """
        self._use_jit = use_jit
        timer = Timer()
        timer.start("Objecive build")

        # build constraints
        self._dim_c = 0
        for constraint in self.constraints:
            if not constraint.linear:
                raise NotImplementedError("Constraints must be linear.")
            if verbose > 0:
                print("Building constraint: " + constraint.name)
            constraint.build(eq, use_jit=self.use_jit, verbose=verbose)
            self._dim_c += constraint.dim_f

        # build objectives
        self._dim_f = 0
        for objective in self.objectives:
            if verbose > 0:
                print("Building objective: " + objective.name)
            objective.build(eq, use_jit=self.use_jit, verbose=verbose)
            self._dim_f += objective.dim_f
        if self._dim_f == 1:
            self._scalar = True
        else:
            self._scalar = False

        self._set_state_vector()

        # build linear constraint matrices
        if verbose > 0:
            print("Building linear constraints")
        timer.start("linear constraint build")
        self._build_linear_constraints()
        timer.stop("linear constraint build")
        if verbose > 1:
            timer.disp("linear constraint build")

        self._set_derivatives(self.use_jit)

        self._built = True
        timer.stop("Objecive build")
        if verbose > 1:
            timer.disp("Objecive build")

    def rebuild_constraints(self, eq):
        """Rebuild the constraints.

        Parameters
        ----------
        eq : Equilibrium, optional
            Equilibrium that will be optimized to satisfy the Objective.

        """
        for constraint in self.constraints:
            constraint.update_target(eq)

        # A*y = b
        self._b = np.array([])
        for obj in self.constraints:
            b = obj.target
            self._b = np.hstack((self.b, b)) if self.b.size else b

        self._b = self._b[self._unfixed_rows]
        temp_y0 = np.dot(self._Ainv, self._b)
        self._y0 = jnp.zeros(self.dim_y)
        self._y0 = put(self._y0, self._unfixed_idx, temp_y0)
        self._y0 = put(self._y0, self._fixed_idx, self._fixed_vals)

    def compute(self, x):
        """Compute the objective function.

        Parameters
        ----------
        x : ndarray
            Optimization variables (x) or full state vector (y).

        Returns
        -------
        f : ndarray
            Objective function value(s).

        """
        kwargs = self.unpack_state(x)
        f = jnp.concatenate([obj.compute(**kwargs) for obj in self.objectives])
        if f.size > 1:
            return f
        else:
            return f[0]

    def compute_scalar(self, x):
        """Compute the scalar form of the objective.

        Parameters
        ----------
        x : ndarray
            Optimization variables (x) or full state vector (y).

        Returns
        -------
        f : float
            Objective function scalar value.

        """
        if self.scalar:
            f = self.compute(x)
        else:
            f = jnp.sum(self.compute(x) ** 2) / 2
        return f

    def callback(self, x):
        """Print the value(s) of the objective.

        Parameters
        ----------
        x : ndarray
            Optimization variables (x) or full state vector (y).

        """
        f = self.compute_scalar(x)
        print("Total (sum of squares): {:10.3e}, ".format(f))
        kwargs = self.unpack_state(x)
        for obj in self.objectives:
            obj.callback(**kwargs)
        return None

    def unpack_state(self, x):
        """Unpack the state vector x (or y) into its components.

        Parameters
        ----------
        x : ndarray
            Optimization variables (x) or full state vector (y).

        Returns
        -------
        kwargs : dict
            Dictionary of the state components with argument names as keys.

        """
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")

        x = jnp.atleast_1d(x)
        if x.size == self.dim_x:
            y = self.recover(x)
        elif x.size == self.dim_y:
            y = x
        else:
            raise ValueError("Input vector dimension is invalid.")

        kwargs = {}
        for arg in self.args:
            kwargs[arg] = y[self.y_idx[arg]]
        return kwargs

    def project(self, y):
        """Project a full state vector y into the optimization vector x."""
        if not self._built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        y0 = self.y0
        dy = (y - y0)[self._unfixed_idx]
        x = jnp.dot(self.Z.T, dy)
        return jnp.squeeze(x)

    def recover(self, x):
        """Recover the full state vector y from the optimization vector x."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        y0 = self.y0
        dy = jnp.zeros(self.dim_y)
        temp_y = jnp.dot(self.Z, x)
        dy = put(dy, self._unfixed_idx, temp_y)
        # dy = put(dy, self._fixed_idx, self._fixed_vals)
        return jnp.squeeze(y0 + dy)

    def make_feasible(self, y):
        """Return a full state vector y that satisfies the linear constraints."""
        x = self.project(y)
        return self.recover(x)

    def y(self, eq):
        """Return the full state vector y from the Equilibrium eq."""
        y = np.zeros((self.dim_y,))
        for arg in self.args:
            y[self.y_idx[arg]] = getattr(eq, arg)
        return y

    def x(self, eq):
        """Return the optimization variable x from the Equilibrium eq."""
        return self.project(self.y(eq))

    def grad(self, x):
        """Compute gradient vector of scalar form of the objective wrt x."""
        # TODO: add block method
        return self._grad.compute(x)

    def hess(self, x):
        """Compute Hessian matrix of scalar form of the objective wrt x."""
        # TODO: add block method
        return self._hess.compute(x)

    def jac(self, x):
        """Compute Jacobian matrx of vector form of the objective wrt x."""
        return self._jac.compute(x)

    def jvp(self, v, x):
        """Compute Jacobian-vector product of the objective function.

        Parameters
        ----------
        v : tuple of ndarray
            Vectors to right-multiply the Jacobian by.
            The number of vectors given determines the order of derivative taken.
        x : ndarray
            Optimization variables.

        """
        if not isinstance(v, tuple):
            v = (v,)
        if len(v) == 1:
            return Derivative.compute_jvp(self.compute, 0, v[0], x)
        elif len(v) == 2:
            return Derivative.compute_jvp2(self.compute, 0, 0, v[0], v[1], x)
        elif len(v) == 3:
            return Derivative.compute_jvp3(self.compute, 0, 0, 0, v[0], v[1], v[2], x)
        else:
            raise NotImplementedError("Cannot compute JVP higher than 3rd order.")

    def compile(self, mode="auto", verbose=1):
        """Call the necessary functions to ensure the function is compiled.

        Parameters
        ----------
        mode : {"auto", "lsq", "scalar", "all"}
            Whether to compile for least squares optimization or scalar optimization.
            "auto" compiles based on the type of objective,
            "all" compiles all derivatives.
        verbose : int, optional
            Level of output.

        """
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        if not use_jax:
            self._compiled = True
            return

        timer = Timer()
        if mode == "auto" and self.scalar:
            mode = "scalar"
        elif mode == "auto":
            mode = "lsq"

        # variable values are irrelevant for compilation
        x = np.zeros((self.dim_x,))
        y0 = self.y0

        if verbose > 0:
            print("Compiling objective function and derivatives")
        timer.start("Total compilation time")

        if mode in ["scalar", "all"]:
            timer.start("Objective compilation time")
            f0 = self.compute_scalar(x).block_until_ready()
            timer.stop("Objective compilation time")
            if verbose > 1:
                timer.disp("Objective compilation time")
            timer.start("Gradient compilation time")
            g0 = self.grad(x).block_until_ready()
            timer.stop("Gradient compilation time")
            if verbose > 1:
                timer.disp("Gradient compilation time")
            timer.start("Hessian compilation time")
            H0 = self.hess(x).block_until_ready()
            timer.stop("Hessian compilation time")
            if verbose > 1:
                timer.disp("Hessian compilation time")
        if mode in ["lsq", "all"]:
            timer.start("Objective compilation time")
            f0 = self.compute(x).block_until_ready()
            timer.stop("Objective compilation time")
            if verbose > 1:
                timer.disp("Objective compilation time")
            timer.start("Jacobian compilation time")
            J0 = self.jac(x).block_until_ready()
            timer.stop("Jacobian compilation time")
            if verbose > 1:
                timer.disp("Jacobian compilation time")

        timer.stop("Total compilation time")
        if verbose > 1:
            timer.disp("Total compilation time")
        self._compiled = True

    @property
    def objectives(self):
        """list: List of objectives."""
        return self._objectives

    @property
    def constraints(self):
        """list: List of constraints."""
        return self._constraints

    @property
    def use_jit(self):
        """bool: Whether to just-in-time compile the objective and derivatives."""
        return self._use_jit

    @property
    def scalar(self):
        """bool: Whether default "compute" method is a scalar (or vector)."""
        if not self._built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._scalar

    @property
    def built(self):
        """bool: Whether the objectives have been built (or not)."""
        return self._built

    @property
    def compiled(self):
        """bool: Whether the functions have been compiled (or not)."""
        return self._compiled

    @property
    def args(self):
        """list: Names (str) of arguments to the compute functions."""
        return self._args

    @property
    def dimensions(self):
        """dict: Dimensions of the argument given by the dict keys."""
        return self._dimensions

    @property
    def b_idx(self):
        """dict: Indicies of the components of the constraint vector b."""
        return self._b_idx

    @property
    def y_idx(self):
        """dict: Indicies of the components of the full state vector y."""
        return self._y_idx

    @property
    def dim_y(self):
        """int: Dimensional of the full state vector y."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._dim_y

    @property
    def dim_x(self):
        """int: Dimension of the optimization vector x."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._dim_x

    @property
    def dim_c(self):
        """int: Number of constraint equations."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._dim_c

    @property
    def dim_f(self):
        """int: Number of objective equations."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._dim_f

    @property
    def A(self):
        """ndarray: Linear constraint matrix: A*y = b."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._A

    @property
    def Ainv(self):
        """ndarray: Linear constraint matrix inverse: y0 = Ainv*b."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._Ainv

    @property
    def b(self):
        """ndarray: Linear constraint vector: A*y = b."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._b

    @property
    def y0(self):
        """ndarray: Feasible state vector."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._y0

    @property
    def Z(self):
        """ndarray: Linear constraint nullspace: y = y0 + Z*x, dy/dx = Z."""
        if not self.built:
            raise RuntimeError("ObjectiveFunction must be built first.")
        return self._Z


class _Objective(IOAble, ABC):
    """Objective (or constraint) used in the optimization of an Equilibrium."""

    _io_attrs_ = ["_target", "_weight", "_name"]

    def __init__(self, eq=None, target=0, weight=1, name=None):
        """Initialize an Objective.

        Parameters
        ----------
        eq : Equilibrium, optional
            Equilibrium that will be optimized to satisfy the Objective.
        target : float, ndarray
            Target value(s) of the objective.
            len(target) must be equal to Objective.dim_f
        weight : float, ndarray, optional
            Weighting to apply to the Objective, relative to other Objectives.
            len(weight) must be equal to Objective.dim_f
        name : str
            Name of the objective function.

        """
        assert np.all(np.asarray(weight) > 0)
        self._target = np.atleast_1d(target)
        self._weight = np.atleast_1d(weight)
        self._name = name
        self._built = False

        if eq is not None:
            self.build(eq)

    def _set_dimensions(self, eq):
        """Set state vector component dimensions."""
        self._dimensions = {}
        self._dimensions["R_lmn"] = eq.R_basis.num_modes
        self._dimensions["Z_lmn"] = eq.Z_basis.num_modes
        self._dimensions["L_lmn"] = eq.L_basis.num_modes
        self._dimensions["Rb_lmn"] = eq.surface.R_basis.num_modes
        self._dimensions["Zb_lmn"] = eq.surface.Z_basis.num_modes
        self._dimensions["p_l"] = eq.pressure.params.size
        self._dimensions["i_l"] = eq.iota.params.size
        self._dimensions["Psi"] = 1

    def _set_derivatives(self, use_jit=True):
        """Set up derivatives of the objective wrt each argument."""
        self._derivatives = {}
        self._scalar_derivatives = {}
        self._args = getfullargspec(self.compute)[0][1:]

        # only used for linear objectives so variable values are irrelevant
        kwargs = {
            "R_lmn": np.zeros((self.dimensions["R_lmn"],)),
            "Z_lmn": np.zeros((self.dimensions["Z_lmn"],)),
            "L_lmn": np.zeros((self.dimensions["L_lmn"],)),
            "Rb_lmn": np.zeros((self.dimensions["Rb_lmn"],)),
            "Zb_lmn": np.zeros((self.dimensions["Zb_lmn"],)),
            "p_l": np.zeros((self.dimensions["p_l"],)),
            "i_l": np.zeros((self.dimensions["i_l"],)),
            "Psi": np.zeros((self.dimensions["Psi"],)),
        }
        args = [kwargs[arg] for arg in self.args]

        # constant derivatives are pre-computed, otherwise set up Derivative instance
        for arg in arg_order:
            if arg in self.args:  # derivative wrt arg
                self._derivatives[arg] = Derivative(
                    self.compute,
                    argnum=self.args.index(arg),
                    mode="fwd",
                    use_jit=use_jit,
                )
                self._scalar_derivatives[arg] = Derivative(
                    self.compute_scalar,
                    argnum=self.args.index(arg),
                    mode="fwd",
                    use_jit=use_jit,
                )
                if self.linear:  # linear objectives have constant derivatives
                    self._derivatives[arg] = self._derivatives[arg].compute(*args)
                    self._scalar_derivatives[arg] = self._scalar_derivatives[
                        arg
                    ].compute(*args)
            else:  # these derivatives are always zero
                self._derivatives[arg] = np.zeros((self.dim_f, self.dimensions[arg]))
                self._scalar_derivatives[arg] = np.zeros((1, self.dimensions[arg]))

        if use_jit:
            self.compute = jit(self.compute)
            self.compute_scalar = jit(self.compute_scalar)

    def _check_dimensions(self):
        """Check that len(target) = len(weight) = dim_f."""
        if np.unique(self.target).size == 1:
            self._target = np.repeat(self.target[0], self.dim_f)
        if np.unique(self.weight).size == 1:
            self._weight = np.repeat(self.weight[0], self.dim_f)

        if self.target.size != self.dim_f:
            raise ValueError("len(target) != dim_f")
        if self.weight.size != self.dim_f:
            raise ValueError("len(weight) != dim_f")

        return None

    def update_target(self, eq):
        """Update target values using an Equilibrium.

        Parameters
        ----------
        eq : Equilibrium
            Equilibrium that will be optimized to satisfy the Objective.

        """
        self.target = np.atleast_1d(getattr(eq, self.target_arg, self.target))

    @abstractmethod
    def build(self, eq, use_jit=True, verbose=1):
        """Build constant arrays."""

    @abstractmethod
    def compute(self, *args, **kwargs):
        """Compute the objective function."""

    def compute_scalar(self, *args, **kwargs):
        """Compute the scalar form of the objective."""
        if self.scalar:
            f = self.compute(*args, **kwargs)
        else:
            f = jnp.sum(self.compute(*args, **kwargs) ** 2) / 2
        return f

    def callback(self, *args, **kwargs):
        """Print the value of the objective."""
        x = self._unshift_unscale(self.compute(*args, **kwargs))
        print(self._callback_fmt.format(jnp.linalg.norm(x)))

    def _shift_scale(self, x):
        """Apply target and weighting."""
        return (x - self.target) * self.weight

    def _unshift_unscale(self, x):
        """Undo target and weighting."""
        return x / self.weight + self.target

    @property
    def target(self):
        """float: Target value(s) of the objective."""
        return self._target

    @target.setter
    def target(self, target):
        self._target = np.atleast_1d(target)
        self._check_dimensions()

    @property
    def weight(self):
        """float: Weighting to apply to the Objective, relative to other Objectives."""
        return self._weight

    @weight.setter
    def weight(self, weight):
        assert np.all(np.asarray(weight) > 0)
        self._weight = np.atleast_1d(weight)
        self._check_dimensions()

    @property
    def built(self):
        """bool: Whether the transforms have been precomputed (or not)."""
        return self._built

    @property
    def args(self):
        """list: Names (str) of arguments to the compute functions."""
        return self._args

    @property
    def target_arg(self):
        """str: Name of argument corresponding to the target."""
        return ""

    @property
    def dimensions(self):
        """dict: Dimensions of the argument given by the dict keys."""
        return self._dimensions

    @property
    def derivatives(self):
        """dict: Derivatives of the function wrt the argument given by the dict keys."""
        return self._derivatives

    @property
    def dim_f(self):
        """int: Number of objective equations."""
        return self._dim_f

    @property
    def scalar(self):
        """bool: Whether default "compute" method is a scalar (or vector)."""
        return self._scalar

    @property
    def linear(self):
        """bool: Whether the objective is a linear function (or nonlinear)."""
        return self._linear

    @property
    def name(self):
        """Name of objective function (str)."""
        return self._name