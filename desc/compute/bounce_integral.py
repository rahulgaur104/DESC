"""Methods for computing bounce integrals."""

from functools import partial

from interpax import CubicHermiteSpline, PchipInterpolator, PPoly, interp1d
from matplotlib import pyplot as plt

from desc.backend import complex_sqrt, flatnonzero, jnp, put_along_axis, take
from desc.compute.utils import safediv
from desc.equilibrium.coords import desc_grid_from_field_line_coords
from desc.utils import errorif


@partial(jnp.vectorize, signature="(m),(m)->(n)", excluded={2, 3})
def take_mask(a, mask, size=None, fill_value=None):
    """JIT compilable method to return ``a[mask][:size]`` padded by ``fill_value``.

    Parameters
    ----------
    a : Array
        The source array.
    mask : Array
        Boolean mask to index into ``a``. Should have same shape as ``a``.
    size : int
        Elements of ``a`` at the first size True indices of ``mask`` will be returned.
        If there are fewer elements than size indicates, the returned array will be
        padded with fill_value. Defaults to ``mask.size``.
    fill_value :
        When there are fewer than the indicated number of elements,
        the remaining elements will be filled with ``fill_value``.
        Defaults to NaN for inexact types,
        the largest negative value for signed types,
        the largest positive value for unsigned types,
        and True for booleans.

    Returns
    -------
    a[mask][:size] : Array, shape(size, )
        Output array.

    """
    assert a.shape == mask.shape
    idx = flatnonzero(
        mask, size=mask.size if size is None else size, fill_value=mask.size
    )
    return take(
        a,
        idx,
        mode="fill",
        fill_value=fill_value,
        unique_indices=True,
        indices_are_sorted=True,
    )


# only use for debugging
def _filter_not_nan(a):
    """Filter out nan from ``a`` while asserting nan is padded at right."""
    is_nan = jnp.isnan(a)
    assert jnp.array_equal(is_nan, jnp.sort(is_nan, axis=-1)), "take_mask() has a bug."
    return a[~is_nan]


def _filter_real(a, a_min=-jnp.inf, a_max=jnp.inf):
    """Keep real values inside [a_min, a_max] and set others to nan.

    Parameters
    ----------
    a : Array
        Complex-valued array.
    a_min, a_max : Array, Array
        Minimum and maximum value to keep real values between.
        Should broadcast with ``a``.

    Returns
    -------
    roots : Array
        The real values of ``a`` in [``a_min``, ``a_max``]; others set to nan.
        The returned array preserves the order of ``a``.

    """
    if a_min is None:
        a_min = -jnp.inf
    if a_max is None:
        a_max = jnp.inf
    return jnp.where(
        jnp.isclose(jnp.imag(a), 0) & (a_min <= a) & (a <= a_max),
        jnp.real(a),
        jnp.nan,
    )


def _root_linear(a, b, distinct=False):
    """Return r such that a r + b = 0."""
    return safediv(-b, a, fill=jnp.where(jnp.isclose(b, 0), 0, jnp.nan))


def _root_quadratic(a, b, c, distinct=False):
    """Return r such that a r² + b r + c = 0."""
    discriminant = b**2 - 4 * a * c
    C = complex_sqrt(discriminant)

    def root(xi):
        return safediv(-b + xi * C, 2 * a)

    is_linear = jnp.isclose(a, 0)
    suppress_root = distinct & jnp.isclose(discriminant, 0)
    r1 = jnp.where(is_linear, _root_linear(b, c), root(-1))
    r2 = jnp.where(is_linear | suppress_root, jnp.nan, root(1))
    return r1, r2


def _root_cubic(a, b, c, d, distinct=False):
    """Return r such that a r³ + b r² + c r + d = 0."""
    # https://en.wikipedia.org/wiki/Cubic_equation#General_cubic_formula
    t_0 = b**2 - 3 * a * c
    t_1 = 2 * b**3 - 9 * a * b * c + 27 * a**2 * d
    discriminant = t_1**2 - 4 * t_0**3
    C = ((t_1 + complex_sqrt(discriminant)) / 2) ** (1 / 3)
    C_is_zero = jnp.isclose(C, 0)

    def root(xi):
        return safediv(b + xi * C + jnp.where(C_is_zero, 0, t_0 / (xi * C)), -3 * a)

    xi0 = 1
    xi1 = (-1 + (-3) ** 0.5) / 2
    xi2 = xi1**2
    is_quadratic = jnp.isclose(a, 0)
    # C = 0 is equivalent to existence of triple root.
    # Assuming the coefficients are real, it is also equivalent to
    # existence of any real roots with multiplicity > 1.
    suppress_root = distinct & C_is_zero
    q1, q2 = _root_quadratic(b, c, d, distinct)
    r1 = jnp.where(is_quadratic, q1, root(xi0))
    r2 = jnp.where(is_quadratic, q2, jnp.where(suppress_root, jnp.nan, root(xi1)))
    r3 = jnp.where(is_quadratic | suppress_root, jnp.nan, root(xi2))
    return r1, r2, r3


_roots = jnp.vectorize(partial(jnp.roots, strip_zeros=False), signature="(m)->(n)")


def _poly_root(c, k=0, a_min=None, a_max=None, sort=False, distinct=False):
    """Roots of polynomial with given coefficients.

    Parameters
    ----------
    c : Array
        First axis should store coefficients of a polynomial.
        For a polynomial given by ∑ᵢⁿ cᵢ xⁱ, where n is ``c.shape[0] - 1``,
        coefficient cᵢ should be stored at ``c[n - i]``.
    k : Array
        Specify to find solutions to ∑ᵢⁿ cᵢ xⁱ = ``k``.
        Should broadcast with arrays of shape(*c.shape[1:]).
    a_min, a_max : Array, Array
        Minimum and maximum value to return roots between.
        If specified only real roots are returned.
        If None, returns all complex roots.
        Should broadcast with arrays of shape(*c.shape[1:]).
    sort : bool
        Whether to sort the roots.
    distinct : bool
        Whether to only return the distinct roots. If true, when the
        multiplicity is greater than one, the repeated roots are set to nan.

    Returns
    -------
    r : Array, shape(..., c.shape[1:], c.shape[0] - 1)
        The roots of the polynomial, iterated over the last axis.

    """
    keep_only_real = not (a_min is None and a_max is None)
    func = {2: _root_linear, 3: _root_quadratic, 4: _root_cubic}
    if c.shape[0] in func:
        # Compute from analytic formula.
        r = func[c.shape[0]](*c[:-1], c[-1] - k, distinct)
        if keep_only_real:
            r = [_filter_real(rr, a_min, a_max) for rr in r]
        r = jnp.stack(r, axis=-1)
        # We had ignored the case of double complex roots.
        distinct = distinct and c.shape[0] > 3 and not keep_only_real
    else:
        # Compute from eigenvalues of polynomial companion matrix.
        # This method can fail to detect roots near extrema, which is often
        # where we want to detect roots for bounce integrals.
        c_n = c[-1] - k
        c = [jnp.broadcast_to(c_i, c_n.shape) for c_i in c[:-1]]
        c.append(c_n)
        c = jnp.stack(c, axis=-1)
        r = _roots(c)
        if keep_only_real:
            if a_min is not None:
                a_min = a_min[..., jnp.newaxis]
            if a_max is not None:
                a_max = a_max[..., jnp.newaxis]
            r = _filter_real(r, a_min, a_max)
    if sort or distinct:
        r = jnp.sort(r, axis=-1)
    if distinct:
        # Atol needs to be low enough that distinct roots which are close do not
        # get removed, otherwise algorithms that rely on continuity of the spline
        # such as bounce_points() will fail. The current atol was chosen so that
        # test_bounce_points() passes when this block is forced to run.
        mask = jnp.isclose(jnp.diff(r, axis=-1, prepend=jnp.nan), 0, atol=1e-15)
        r = jnp.where(mask, jnp.nan, r)
    return r


def _poly_der(c):
    """Coefficients for the derivatives of the given set of polynomials.

    Parameters
    ----------
    c : Array
        First axis should store coefficients of a polynomial.
        For a polynomial given by ∑ᵢⁿ cᵢ xⁱ, where n is ``c.shape[0] - 1``,
        coefficient cᵢ should be stored at ``c[n - i]``.

    Returns
    -------
    poly : Array
        Coefficients of polynomial derivative, ignoring the arbitrary constant.
        That is, ``poly[i]`` stores the coefficient of the monomial xⁿ⁻ⁱ⁻¹,
        where n is ``c.shape[0] - 1``.

    """
    poly = (c[:-1].T * jnp.arange(c.shape[0] - 1, 0, -1)).T
    return poly


def _poly_val(x, c):
    """Evaluate the set of polynomials c at the points x.

    Note that this function does not perform the same operation as
    ``np.polynomial.polynomial.polyval(x, c)``.

    Parameters
    ----------
    x : Array
        Coordinates at which to evaluate the set of polynomials.
    c : Array
        First axis should store coefficients of a polynomial.
        For a polynomial given by ∑ᵢⁿ cᵢ xⁱ, where n is ``c.shape[0] - 1``,
        coefficient cᵢ should be stored at ``c[n - i]``.

    Returns
    -------
    val : Array
        Polynomial with given coefficients evaluated at given points.

    Examples
    --------
    .. code-block:: python

        val = polyval(x, c)
        if val.ndim != max(x.ndim, c.ndim - 1):
            raise ValueError(f"Incompatible shapes {x.shape} and {c.shape}.")
        for index in np.ndindex(c.shape[1:]):
            idx = (..., *index)
            np.testing.assert_allclose(
                actual=val[idx],
                desired=np.poly1d(c[idx])(x[idx]),
                err_msg=f"Failed with shapes {x.shape} and {c.shape}.",
            )

    """
    # Fine instead of Horner's method as we expect to evaluate cubic polynomials.
    X = x[..., jnp.newaxis] ** jnp.arange(c.shape[0] - 1, -1, -1)
    val = jnp.einsum("...i,i...->...", X, c)
    return val


def composite_linspace(knots, resolution, is_sorted=False):
    """Returns linearly spaced points between ``knots``.

    Parameters
    ----------
    knots : Array
        First axis has values to return linearly spaced values between.
        The remaining axes are batch axes.
    resolution : int
        Number of points between each knot.
    is_sorted : bool
        Whether the knots are already sorted along the first axis.

    Returns
    -------
    result : Array, shape((knots.shape[0] - 1) * resolution + 1, *knots.shape[1:])
        Sorted linearly spaced points between ``knots``.

    """
    knots = jnp.atleast_1d(knots)
    P = knots.shape[0]
    S = knots.shape[1:]
    if not is_sorted:
        knots = jnp.sort(knots, axis=0)
    result = jnp.linspace(knots[:-1, ...], knots[1:, ...], resolution, endpoint=False)
    result = jnp.moveaxis(result, source=0, destination=1).reshape(-1, *S)
    result = jnp.append(result, knots[jnp.newaxis, -1, ...], axis=0)
    assert result.shape == ((P - 1) * resolution + 1, *S)
    return result


def _check_shape(knots, B_c, B_z_ra_c, pitch=None):
    """Ensure inputs have compatible shape, and return them with full dimension.

    Parameters
    ----------
    knots : Array, shape(knots.size, )
        Field line-following ζ coordinates of spline knots.

    Returns
    -------
    B_c : Array, shape(B_c.shape[0], S, knots.size - 1)
        Polynomial coefficients of the spline of |B| in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    B_z_ra_c : Array, shape(B_c.shape[0] - 1, *B_c.shape[1:])
        Polynomial coefficients of the spline of ∂|B|/∂_ζ in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    pitch : Array, shape(P, S)
        λ values.
        λ(ρ, α) is specified by ``pitch[..., (ρ, α)]``
        where in the latter the labels (ρ, α) are interpreted as index into the
        last axis that corresponds to that field line.
        If two-dimensional, the first axis is the batch axis as usual.

    """
    errorif(knots.ndim != 1)
    if B_c.ndim == 2 and B_z_ra_c.ndim == 2:
        # Add axis which enumerates field lines.
        B_c = B_c[:, jnp.newaxis]
        B_z_ra_c = B_z_ra_c[:, jnp.newaxis]
    msg = "Supplied invalid shape for splines."
    errorif(not (B_c.ndim == B_z_ra_c.ndim == 3), msg=msg)
    errorif(B_c.shape[0] - 1 != B_z_ra_c.shape[0], msg=msg)
    errorif(B_c.shape[1:] != B_z_ra_c.shape[1:], msg=msg)
    msg = "Last axis fails to enumerate spline polynomials."
    errorif(B_c.shape[-1] != knots.size - 1, msg=msg)
    if pitch is not None:
        pitch = jnp.atleast_2d(pitch)
        msg = "Supplied invalid shape for pitch angles."
        errorif(pitch.ndim != 2, msg=msg)
        errorif(pitch.shape[-1] != 1 and pitch.shape[-1] != B_c.shape[1], msg=msg)
    return B_c, B_z_ra_c, pitch


def pitch_of_extrema(knots, B_c, B_z_ra_c, relative_shift=1e-6):
    """Return pitch values that will capture fat banana orbits.

    Particles with λ = 1 / |B|(ζ*) where |B|(ζ*) are local maxima
    have fat banana orbits increasing neoclassical transport.

    When computing ε ∼ ∫ db ∑ⱼ Hⱼ² / Iⱼ in equation 29 of

        V. V. Nemov, S. V. Kasilov, W. Kernbichler, M. F. Heyn.
        Evaluation of 1/ν neoclassical transport in stellarators.
        Phys. Plasmas 1 December 1999; 6 (12): 4622–4632.
        https://doi.org/10.1063/1.873749

    the contribution of ∑ⱼ Hⱼ² / Iⱼ to ε is largest in the intervals such that
    b ∈ [|B|(ζ*) - db, |B|(ζ*)]. To see this, observe that Iⱼ ∼ √(1 − λ B),
    hence Hⱼ² / Iⱼ ∼ Hⱼ² / √(1 − λ B). For λ = 1 / |B|(ζ*), near |B|(ζ*), the
    quantity 1 / √(1 − λ B) is singular. The slower |B| tends to |B|(ζ*) the
    less integrable this singularity becomes. Therefore, a quadrature for
    ε ∼ ∫ db ∑ⱼ Hⱼ² / Iⱼ would do well to evaluate the integrand near
    b = 1 / λ = |B|(ζ*).

    Parameters
    ----------
    knots : Array, shape(knots.size, )
        Field line-following ζ coordinates of spline knots.
    B_c : Array, shape(B_c.shape[0], S, knots.size - 1)
        Polynomial coefficients of the spline of |B| in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    B_z_ra_c : Array, shape(B_c.shape[0] - 1, *B_c.shape[1:])
        Polynomial coefficients of the spline of ∂|B|/∂_ζ in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    relative_shift : float
        Relative amount to shift maxima down and minima up to avoid floating point
        errors in downstream routines.

    Returns
    -------
    pitch : Array, shape(N * (degree - 1), S)
        For the shaping notation, the ``degree`` of the spline of |B| matches
        ``B_c.shape[0] - 1``, the number of polynomials per spline ``N`` matches
        ``knots.size - 1``, and the number of field lines is denoted by ``S``.

        If there were less than ``N * (degree - 1)`` extrema detected along a
        field line, then the first axis, which enumerates the pitch values for
        a particular field line, is padded with nan.

    """
    B_c, B_z_ra_c, _ = _check_shape(knots, B_c, B_z_ra_c)
    S, N, degree = B_c.shape[1], knots.size - 1, B_c.shape[0] - 1
    extrema = _poly_root(
        c=B_z_ra_c, a_min=jnp.array([0]), a_max=jnp.diff(knots), distinct=True
    )
    # Can detect at most degree of |B|_z_ra spline extrema between each knot.
    assert extrema.shape == (S, N, degree - 1)
    B_extrema = _poly_val(x=extrema, c=B_c[..., jnp.newaxis])
    B_extrema_z_ra = _poly_val(x=extrema, c=_poly_der(B_z_ra_c)[..., jnp.newaxis])
    # Floating point error impedes consistent detection of bounce points riding
    # extrema. Shift pitch values slightly to resolve this issue.
    # Higher priority to shift down maxima than shift up minima, so identify near
    # equality with zero as maxima.
    is_maxima = B_extrema_z_ra <= 0
    B_extrema = jnp.where(
        is_maxima,
        (1 - relative_shift) * B_extrema,
        (1 + relative_shift) * B_extrema,
    ).reshape(S, -1)
    # Reshape so that last axis enumerates extrema along a field line.
    # Pad all the nan at the end rather than interspersed to be consistent.
    B_extrema = take_mask(B_extrema, ~jnp.isnan(B_extrema))
    pitch = 1 / B_extrema.T
    assert pitch.shape == (N * (degree - 1), S)
    return pitch


def bounce_points(pitch, knots, B_c, B_z_ra_c, check=False, plot=True):
    """Compute the bounce points given spline of |B| and pitch λ.

    Parameters
    ----------
    pitch : Array, shape(P, S)
        λ values.
        λ(ρ, α) is specified by ``pitch[..., (ρ, α)]``
        where in the latter the labels (ρ, α) are interpreted as index into the
        last axis that corresponds to that field line.
        If two-dimensional, the first axis is the batch axis as usual.
    knots : Array, shape(knots.size, )
        Field line-following ζ coordinates of spline knots.
    B_c : Array, shape(B_c.shape[0], S, knots.size - 1)
        Polynomial coefficients of the spline of |B| in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    B_z_ra_c : Array, shape(B_c.shape[0] - 1, *B_c.shape[1:])
        Polynomial coefficients of the spline of ∂|B|/∂_ζ in local power basis.
        First axis enumerates the coefficients of power series.
        Second axis enumerates the splines along the field lines.
        Last axis enumerates the polynomials of the spline along a particular
        field line.
    check : bool
        Flag for debugging.
    plot : bool
        Whether to plot even if error was not detected during the check.

    Returns
    -------
    bp1, bp2 : Array, Array, shape(P, S, N * degree)
        For the shaping notation, the ``degree`` of the spline of |B| matches
        ``B_c.shape[0] - 1``, the number of polynomials per spline ``N`` matches
        ``knots.size - 1``, and the number of field lines is denoted by ``S``.

        The returned arrays are the field line-following ζ coordinates of bounce
        points for a given pitch along a field line. The pairs bp1[i, j, k] and
        bp2[i, j, k] form left and right integration boundaries, respectively,
        for the bounce integrals. If there were less than ``N * degree`` bounce
        points detected along a field line, then the last axis, which enumerates
        the bounce points for a particular field line, is padded with nan.

    """
    B_c, B_z_ra_c, pitch = _check_shape(knots, B_c, B_z_ra_c, pitch)
    P, S, N, degree = pitch.shape[0], B_c.shape[1], knots.size - 1, B_c.shape[0] - 1
    # The polynomials' intersection points with 1 / λ is given by ``intersect``.
    # In order to be JIT compilable, this must have a shape that accommodates the
    # case where each polynomial intersects 1 / λ degree times.
    # nan values in ``intersect`` denote a polynomial has less than degree intersects.
    intersect = _poly_root(
        c=B_c,
        # New axis to use same pitches across polynomials of a particular spline.
        k=(1 / pitch)[..., jnp.newaxis],
        a_min=jnp.array([0]),
        a_max=jnp.diff(knots),
        sort=True,
        distinct=True,
    )
    assert intersect.shape == (P, S, N, degree)

    # Reshape so that last axis enumerates intersects of a pitch along a field line.
    B_z_ra = _poly_val(x=intersect, c=B_z_ra_c[..., jnp.newaxis]).reshape(P, S, -1)
    # Transform out of local power basis expansion.
    intersect = intersect + knots[:-1, jnp.newaxis]
    intersect = intersect.reshape(P, S, -1)

    # Only consider intersect if it is within knots that bound that polynomial.
    is_intersect = ~jnp.isnan(intersect)
    # Reorder so that all intersects along a field line are contiguous.
    intersect = take_mask(intersect, is_intersect)
    B_z_ra = take_mask(B_z_ra, is_intersect)
    assert intersect.shape == B_z_ra.shape == (P, S, N * degree)
    # Sign of derivative determines whether an intersect is a valid bounce point.
    # Need to include zero derivative intersects to compute the WFB
    # (world's fattest banana) orbit bounce integrals.
    is_bp1 = B_z_ra <= 0
    is_bp2 = B_z_ra >= 0
    # The pairs bp1[i, j, k] and bp2[i, j, k] are boundaries of an integral only
    # if bp1[i, j, k] <= bp2[i, j, k]. For correctness of the algorithm, it is
    # required that the first intersect satisfies non-positive derivative. Now,
    # because B_z_ra[i, j, k] <= 0 implies B_z_ra[i, j, k + 1] >= 0 by continuity,
    # there can be at most one inversion, and if it exists, the inversion must be
    # at the first pair. To correct the inversion, it suffices to disqualify the
    # first intersect as a right boundary, except under the following edge case.
    edge_case = (B_z_ra[..., 0] == 0) & (B_z_ra[..., 1] < 0)
    # In theory, we need to keep propagating this edge case,
    # e.g (B_z_ra[..., 1] < 0) | ((B_z_ra[..., 1] == 0) & (B_z_ra[..., 2] < 0)...).
    # At each step, the likelihood that an intersection has already been lost
    # due to floating point errors grows, so the real solution is to pick a less
    # degenerate pitch value - one that does not ride the global extrema of |B|.
    is_bp2 = put_along_axis(is_bp2, jnp.array(0), edge_case, axis=-1)

    # Get ζ values of bounce points from the masks.
    bp1 = take_mask(intersect, is_bp1)
    bp2 = take_mask(intersect, is_bp2)
    # Consistent with (in particular the discussion on page 3 and 5 of)
    # V. V. Nemov, S. V. Kasilov, W. Kernbichler, M. F. Heyn.
    # Evaluation of 1/ν neoclassical transport in stellarators.
    # Phys. Plasmas 1 December 1999; 6 (12): 4622–4632.
    # https://doi.org/10.1063/1.873749.
    # we ignore the bounce points of particles assigned to a class that
    # are trapped outside this snapshot of the field line. The caveat
    # is that the field line discussed in the paper above specifies the
    # flux surface completely as its length tends to infinity, whereas
    # the field line snapshot here is for a particular alpha coordinate.
    # Don't think it's necessary to stitch together the field lines using
    # rotational transform to potentially capture the bounce point outside
    # this snapshot of the field line.
    if check:
        _check_bounce_points(bp1, bp2, pitch, knots, B_c, plot)
    return bp1, bp2


def _check_bounce_points(bp1, bp2, pitch, knots, B_c, plot=False):
    """Check that bounce points are computed correctly.

    Parameters
    ----------
    bp1, bp2 : Array, Array
        Output of ``bounce_points``.
    pitch : Array
        Input to ``bounce_points``.
    knots : Array
        Input to ``bounce_points``.
    B_c : Array
        Input to ``bounce_points``.
    plot : bool
        Whether to plot even if error was not detected.

    """
    eps = 10 * jnp.finfo(jnp.array(1.0)).eps
    P, S = bp1.shape[:-1]

    msg_1 = "Bounce points have an inversion."
    err_1 = jnp.any(bp1 > bp2, axis=(0, -1))
    msg_2 = "Discontinuity detected."
    err_2 = jnp.any(bp1[..., 1:] < bp2[..., :-1], axis=(0, -1))

    for s in jnp.nonzero(err_1 | err_2)[0]:
        B = PPoly(B_c[:, s], knots)
        for p in range(P):
            B_mid = B((bp1[p, s] + bp2[p, s]) / 2)
            err_1_ps = jnp.any(bp1[p, s] > bp2[p, s])
            err_2_ps = jnp.any(bp1[p, s, 1:] < bp2[p, s, :-1])
            err_3_ps = jnp.any(B_mid > 1 / pitch[p, s] + eps)
            if err_1_ps or err_2_ps or err_3_ps:
                print(f"Error at index p={p}, s={s} out of {P},{S}.")
                bp1_ps, bp2_ps, B_mid = map(
                    _filter_not_nan, (bp1[p, s], bp2[p, s], B_mid)
                )
                print("bp1:        ", bp1_ps)
                print("bp2:        ", bp2_ps)
                print("B - 1/pitch:", B(bp1_ps) - 1 / pitch[p, s])
                plot_field_line_with_ripple(
                    B, pitch[p, s], bp1_ps, bp2_ps, id=f"{p},{s}"
                )
                assert not err_1_ps, msg_1
                assert not err_2_ps, msg_2
                msg_3 = f"B midpoint = {B_mid} > {1 / pitch[p, s] + eps} = 1/pitch."
                assert not err_3_ps, msg_3
    if plot:
        for s in range(S):
            B = PPoly(B_c[:, s], knots)
            plot_field_line_with_ripple(B, pitch[:, s], bp1[:, s], bp2[:, s], id=str(s))


def plot_field_line_with_ripple(
    B,
    pitch=None,
    bp1=jnp.array([]),
    bp2=jnp.array([]),
    start=None,
    stop=None,
    num=300,
    show=True,
    id=None,
):
    """Plot the field line given spline of |B| and bounce points etc.

    Parameters
    ----------
    B : PPoly
        Spline of |B| over given field line.
    pitch : Array
        λ value.
    bp1 : Array
        Bounce points with B_z_ra <= 0.
    bp2 : Array
        Bounce points with B_z_ra >= 0.
    start : float
        Minimum ζ on plot.
    stop : float
        Maximum ζ of plot.
    num : int
        Number of ζ points to plot.
        Should be dense to see oscillations.
    show : bool
        Whether to show the plot.
    id : str
        String to prepend to plot title.

    Returns
    -------
    fig, ax : matplotlib figure and axes.

    """
    legend = {}

    def add(lines):
        if not hasattr(lines, "__iter__"):
            lines = [lines]
        for line in lines:
            label = line.get_label()
            if label not in legend:
                legend[label] = line

    fig, ax = plt.subplots()
    for knot in B.x:
        add(ax.axvline(x=knot, color="tab:blue", alpha=0.25, label="knot"))
    z = jnp.linspace(
        start=B.x[0] if start is None else start,
        stop=B.x[-1] if stop is None else stop,
        num=num,
    )
    add(ax.plot(z, B(z), label=r"$\vert B \vert (\zeta)$"))

    if pitch is not None:
        b = jnp.atleast_1d(1 / pitch)
        for val in jnp.unique(b):
            add(ax.axhline(val, color="tab:purple", alpha=0.75, label=r"$1 / \lambda$"))
        bp1, bp2 = map(jnp.atleast_2d, (bp1, bp2))
        for i in range(bp1.shape[0]):
            bp1_i, bp2_i = map(_filter_not_nan, (bp1[i], bp2[i]))
            add(
                ax.scatter(
                    bp1_i,
                    jnp.full_like(bp1_i, b[i]),
                    marker="v",
                    color="tab:red",
                    label="bp1",
                )
            )
            add(
                ax.scatter(
                    bp2_i,
                    jnp.full_like(bp2_i, b[i]),
                    marker="^",
                    color="tab:green",
                    label="bp2",
                )
            )

    ax.set_xlabel(r"Field line $\zeta$")
    ax.set_ylabel(r"$\vert B \vert \sim 1 / \lambda$")
    ax.legend(legend.values(), legend.keys())
    title = r"Computed bounce points for $\vert B \vert$ and pitch $\lambda$"
    if id is not None:
        title = f"{title}. id = {id}."
    ax.set_title(title)
    if show:
        plt.tight_layout()
        plt.show()
        plt.close()
    return fig, ax


def _affine_bijection_forward(x, a, b):
    """[a, b] ∋ x ↦ y ∈ [−1, 1]."""
    y = 2 * (x - a) / (b - a) - 1
    return y


def affine_bijection_reverse(x, a, b):
    """[−1, 1] ∋ x ↦ y ∈ [a, b]."""
    y = (x + 1) / 2 * (b - a) + a
    return y


def grad_affine_bijection_reverse(a, b):
    """Gradient of reverse affine bijection."""
    dy_dx = (b - a) / 2
    return dy_dx


def automorphism_arcsin(x):
    """[-1, 1] ∋ x ↦ y ∈ [−1, 1].

    The arcsin automorphism is an expansion, so it pushes the evaluation points
    of the bounce integrand toward the singular region, which may induce
    floating point error.

    The gradient of the arcsin automorphism introduces a singularity that augments
    the singularity in the bounce integral. Therefore, the quadrature scheme
    used to evaluate the integral must work well on singular integrals.

    Parameters
    ----------
    x : Array

    Returns
    -------
    y : Array

    """
    y = 2 * jnp.arcsin(x) / jnp.pi
    return y


def grad_automorphism_arcsin(x):
    """Gradient of arcsin automorphism."""
    dy_dx = 2 / (jnp.sqrt(1 - x**2) * jnp.pi)
    return dy_dx


grad_automorphism_arcsin.__doc__ += "\n" + automorphism_arcsin.__doc__


def automorphism_sin(x):
    """[-1, 1] ∋ x ↦ y ∈ [−1, 1].

    The sin automorphism is a contraction, so it pulls the evaluation points
    of the bounce integrand away from the singular region, inducing less
    floating point error.

    The derivative of the sin automorphism is Lipschitz.
    When this automorphism is used as the change of variable map for the bounce
    integral, the Lipschitzness prevents generation of new singularities.
    Furthermore, its derivative vanishes like the integrand of the elliptic
    integral of the second kind E(φ | 1), suppressing the singularity in the
    bounce integrand.

    Therefore, this automorphism pulls the mass of the bounce integral away
    from the singularities, which should improve convergence of the quadrature
    to the true integral, so long as the quadrature performs better on less
    singular integrands. If the integral was singular to begin with,
    Tanh-Sinh quadrature will still work well. Otherwise, Gauss-Legendre
    quadrature can outperform Tanh-Sinh.

    Parameters
    ----------
    x : Array

    Returns
    -------
    y : Array

    """
    y = jnp.sin(jnp.pi * x / 2)
    return y


def grad_automorphism_sin(x):
    """Gradient of sin automorphism."""
    dy_dx = jnp.pi * jnp.cos(jnp.pi * x / 2) / 2
    return dy_dx


grad_automorphism_sin.__doc__ += "\n" + automorphism_sin.__doc__


def tanh_sinh_quad(resolution, w=lambda x: 1, t_max=None):
    """Tanh-Sinh quadrature.

    Returns quadrature points xₖ and weights Wₖ for the approximate evaluation
    of the integral ∫₋₁¹ w(x) f(x) dx ≈ ∑ₖ Wₖ f(xₖ).

    Parameters
    ----------
    resolution: int
        Number of quadrature points, preferably odd.
    w : callable
        Weight function defined, positive, and continuous on (-1, 1).
    t_max : float
        The positive limit of quadrature points to be mapped.
        Larger limit implies better results, but limited due to overflow in sinh.
        A typical value is 3.14.
        Computed automatically if not supplied.

    Returns
    -------
    x : Array
        Quadrature points.
    W : Array
        Quadrature weights.

    """
    if t_max is None:
        # boundary of integral
        x_max = jnp.array(1.0)
        # subtract machine epsilon with buffer for floating point error
        x_max = x_max - 10 * jnp.finfo(x_max).eps
        # inverse of tanh-sinh transformation
        t_max = jnp.arcsinh(2 * jnp.arctanh(x_max) / jnp.pi)
    kh = jnp.linspace(-t_max, t_max, resolution)
    h = 2 * t_max / (resolution - 1)
    arg = 0.5 * jnp.pi * jnp.sinh(kh)
    x = jnp.tanh(arg)
    # weights for Tanh-Sinh quadrature ∫₋₁¹ f(x) dx ≈ ∑ₖ ωₖ f(xₖ)
    W = 0.5 * jnp.pi * h * jnp.cosh(kh) / jnp.cosh(arg) ** 2
    W = W * w(x)
    return x, W


def _suppress_bad_nan(V):
    """Zero out nan values induced by error.

    Assuming that V is a well-behaved function of some interpolation points Z,
    then V(Z) should evaluate as NaN only if Z is NaN. This condition needs to
    be enforced explicitly due to floating point and interpolation error.

    In the context of bounce integrals, the √(1 − λ |B|) terms necessitate this.
    For interpolation error in |B| may yield λ |B| > 1 at quadrature points
    between bounce points, which is inconsistent with our knowledge of the |B|
    spline on which the bounce points were computed. This inconsistency can
    be more prevalent in the limit the number of quadrature points per bounce
    integration is much greater than the number of knots.

    Parameters
    ----------
    V : Array
        Interpolation values.

    Returns
    -------
    V : Array
        The interpolation values with the bad NaN values set to zero.

    """
    # This simple logic is encapsulated here to make explicit the bug it resolves.
    V = jnp.nan_to_num(V, posinf=jnp.inf, neginf=-jnp.inf)
    return V


def _assert_finite_and_hairy(Z, B_sup_z, B, f, B_z_ra, inner_product):
    """Check that no integrals were lost and the hairy ball theorem is upheld."""
    is_not_quad_point = jnp.isnan(Z)
    # We want quantities to evaluate as finite only at quadrature points
    # for the integrals with boundaries at valid bounce points.
    msg = "Interpolation failed."
    assert jnp.all(jnp.isfinite(B_sup_z) ^ is_not_quad_point), msg
    assert jnp.all(jnp.isfinite(B) ^ is_not_quad_point), msg
    assert jnp.all(jnp.isfinite(B_z_ra)), msg
    for ff in f:
        assert jnp.all(jnp.isfinite(ff) ^ is_not_quad_point), msg

    msg = "|B| has vanished."
    assert not jnp.isclose(B, 0).any(), msg
    assert not jnp.isclose(B_sup_z, 0).any(), msg

    quad_resolution = Z.shape[-1]
    # Number of integrals that we should be computing.
    goal = jnp.sum(1 - is_not_quad_point) // quad_resolution
    # Number of integrals that were actually computed.
    actual = jnp.isfinite(inner_product).sum()
    err_msg = f"Lost {goal - actual} integrals. Likely due to floating point error."
    assert goal == actual, err_msg
    assert jnp.all(jnp.isfinite(inner_product) ^ is_not_quad_point[..., 0]), err_msg


_repeated_docstring = """w : Array, shape(w.size, )
        Quadrature weights.
    integrand : callable
        This callable is the composition operator on the set of functions in ``f``
        that maps the functions in ``f`` to the integrand f(ℓ) in ∫ f(ℓ) dℓ.
        It should accept the items in ``f`` as arguments as well as the additional
        keyword arguments: ``B``, ``pitch``, and ``Z``, where ``Z`` is the set of
        quadrature points. A quadrature will be performed to approximate the
        bounce integral of ``integrand(*f, B=B, pitch=pitch, Z=Z)``.
        Note that any arrays baked into the callable method should broadcast
        with ``Z``.
    f : list or tuple of Array, shape(P, S, knots.size, )
        Arguments to the callable ``integrand``.
        These should be the functions in the integrand of the bounce integral
        evaluated (or interpolated to) the nodes of the returned desc
        coordinate grid.
    B_sup_z : Array, shape(S, knots.size, )
        Contravariant field-line following toroidal component of magnetic field.
    B : Array, shape(S, knots.size, )
        Norm of magnetic field.
    B_z_ra : Array, shape(S, knots.size, )
        Norm of magnetic field derivative with respect to field-line following label.
    pitch : Array, shape(P, S)
        λ values to evaluate the bounce integral at each field line.
        λ(ρ, α) is specified by ``pitch[..., (ρ, α)]``
        where in the latter the labels (ρ, α) are interpreted as index into the
        last axis that corresponds to that field line.
        The first axis is the batch axis as usual.
    knots : Array, shape(knots.size, )
        Field line-following ζ coordinates of spline knots.
    method : str
        Method of interpolation for functions contained in ``f``.
        See https://interpax.readthedocs.io/en/latest/_api/interpax.interp1d.html.
    check : bool
        Flag for debugging.

    """
_delimiter = "Returns"


_interp1d_vec = jnp.vectorize(
    interp1d,
    signature="(m),(n),(n)->(m)",
    excluded={"method", "derivative", "extrap", "period"},
)


@partial(
    jnp.vectorize,
    signature="(m),(n),(n),(n)->(m)",
    excluded={"method", "derivative", "extrap", "period"},
)
def _interp1d_vec_with_df(
    xq,
    x,
    f,
    fx,
    method="cubic",
    derivative=0,
    extrap=False,
    period=None,
):
    return interp1d(xq, x, f, method, derivative, extrap, period, fx=fx)


def _interpolatory_quadrature(
    Z, w, integrand, f, B_sup_z, B, B_z_ra, pitch, knots, method, method_B, check=False
):
    """Interpolate given functions to points Z and perform quadrature.

    Parameters
    ----------
    Z : Array, shape(P, S, Z.shape[2], w.size)
        Quadrature points at field line-following ζ coordinates.

    Returns
    -------
    inner_product : Array, shape(Z.shape[:-1])
        Quadrature for every pitch along every field line.

    """
    assert pitch.ndim == 2
    assert w.ndim == knots.ndim == 1
    assert Z.shape == (pitch.shape[0], B.shape[0], Z.shape[2], w.size)
    assert knots.size == B.shape[-1]
    assert B_sup_z.shape == B.shape == B_z_ra.shape
    # Spline the integrand so that we can evaluate it at quadrature points
    # without expensive coordinate mappings and root finding.
    # Spline each function separately so that the singularity near the bounce
    # points can be captured more accurately than can be by any polynomial.
    shape = Z.shape
    Z_ps = Z.reshape(Z.shape[0], Z.shape[1], -1)
    f = [_interp1d_vec(Z_ps, knots, ff, method=method).reshape(shape) for ff in f]
    B_sup_z = _interp1d_vec(Z_ps, knots, B_sup_z, method=method).reshape(shape)
    # Specify derivative at knots for ≈ cubic hermite interpolation.
    B = _interp1d_vec_with_df(Z_ps, knots, B, B_z_ra, method=method_B).reshape(shape)

    pitch = pitch[..., jnp.newaxis, jnp.newaxis]
    inner_product = jnp.dot(
        _suppress_bad_nan(integrand(*f, B=B, pitch=pitch, Z=Z)) / B_sup_z,
        w,
    )
    if check:
        _assert_finite_and_hairy(Z, B_sup_z, B, f, B_z_ra, inner_product)
    return inner_product


_interpolatory_quadrature.__doc__ = _interpolatory_quadrature.__doc__.replace(
    _delimiter, _repeated_docstring + _delimiter, 1
)


def _bounce_quadrature(
    bp1,
    bp2,
    x,
    w,
    integrand,
    f,
    B_sup_z,
    B,
    B_z_ra,
    pitch,
    knots,
    method="akima",
    method_B="cubic",
    check=False,
):
    """Bounce integrate ∫ f(ℓ) dℓ.

    Parameters
    ----------
    bp1, bp2 : Array, Array
        Each should have shape(P, S, bp1.shape[-1]).
        The field line-following ζ coordinates of bounce points for a given pitch
        along a field line. The pairs bp1[i, j, k] and bp2[i, j, k] form left
        and right integration boundaries, respectively, for the bounce integrals.
    x : Array, shape(w.size, )
        Quadrature points in [-1, 1].

    Returns
    -------
    result : Array, shape(P, S, bp1.shape[-1])
        First axis enumerates pitch values. Second axis enumerates the field
        lines. Last axis enumerates the bounce integrals.

    """
    errorif(x.ndim != 1 or x.shape != w.shape)
    errorif(bp1.ndim != 3 or bp1.shape != bp2.shape)
    pitch = jnp.atleast_2d(pitch)

    S = B.shape[0]
    if not isinstance(f, (list, tuple)):
        f = [f]

    def _group_grid_data_by_field_line(g):
        msg = (
            "Should have at most two dimensions, in which case the first axis "
            "is interpreted as the batch axis, which enumerates the evaluation "
            "of the function at particular pitch values."
        )
        errorif(g.ndim > 2, msg=msg)
        return g.reshape(-1, S, knots.size)

    f = map(_group_grid_data_by_field_line, f)
    Z = affine_bijection_reverse(x, bp1[..., jnp.newaxis], bp2[..., jnp.newaxis])
    # Integrate and complete the change of variable.
    result = _interpolatory_quadrature(
        Z, w, integrand, f, B_sup_z, B, B_z_ra, pitch, knots, method, method_B, check
    ) * grad_affine_bijection_reverse(bp1, bp2)
    assert result.shape == (pitch.shape[0], S, bp1.shape[-1])
    return result


_bounce_quadrature.__doc__ = _bounce_quadrature.__doc__.replace(
    _delimiter, _repeated_docstring + _delimiter, 1
)


def bounce_integral(
    eq,
    rho=jnp.linspace(1e-7, 1, 5),
    alpha=None,
    knots=jnp.linspace(-3 * jnp.pi, 3 * jnp.pi, 40),
    quad=tanh_sinh_quad,
    automorphism=(automorphism_arcsin, grad_automorphism_arcsin),
    B_ref=1,
    L_ref=1,
    check=False,
    plot=True,
    **kwargs,
):
    """Returns a method to compute the bounce integral of any quantity.

    The bounce integral is defined as ∫ f(ℓ) dℓ, where
        dℓ parameterizes the distance along the field line,
        λ is a constant proportional to the magnetic moment over energy,
        |B| is the norm of the magnetic field,
        f(ℓ) is the quantity to integrate along the field line,
        and the boundaries of the integral are bounce points, ζ₁, ζ₂, such that
        (λ |B|)(ζᵢ) = 1.
    Physically, the pitch angle λ is the magnetic moment over the energy
    of particle. For a particle with fixed λ, bounce points are defined to be
    the location on the field line such that the particle's velocity parallel
    to the magnetic field is zero.

    The bounce integral is defined up to a sign.
    We choose the sign that corresponds the particle's guiding center trajectory
    traveling in the direction of increasing field-line-following label.

    Parameters
    ----------
    eq : Equilibrium
        Equilibrium on which the bounce integral is computed.
    rho : Array
        Unique flux surface label coordinates.
    alpha : Array
        Unique field line label coordinates over a constant rho surface.
    knots : Array
        Field line following coordinate values at which to compute a spline
        of the integrand, for every field line in the meshgrid formed from
        rho and alpha specified above.
        The number of knots specifies a grid resolution as increasing the
        number of knots increases the accuracy of representing the integrand
        and the accuracy of the locations of the bounce points.
    quad : callable
        The quadrature scheme used to evaluate the integral.
        The returned quadrature points xₖ and weights wₖ
        should approximate ∫₋₁¹ g(x) dx = ∑ₖ wₖ g(xₖ).
        For the default choice of the automorphism below,
        Tanh-Sinh quadrature works well if the integrand is singular.
        Otherwise, Gauss-Legendre quadrature with the sin automorphism
        can be more competitive.
    automorphism : callable, callable
        The first callable should be an automorphism of the real interval [-1, 1].
        The second callable should be the derivative of the first.
        The inverse of the supplied automorphism is composed with the affine
        bijection that maps the bounce points to [-1, 1]. The resulting map
        defines a change of variable for the bounce integral. The choice made
        for the automorphism can augment or suppress singularities.
        Keep this in mind when choosing the quadrature method.
    B_ref : float
        Reference magnetic field strength for normalization.
    L_ref : float
        Reference length scale for normalization.
    check : bool
        Flag for debugging.
    plot : bool
        Whether to plot even if error was not detected during the check.
    kwargs
        Can specify additional arguments to the ``quad`` method with kwargs.
        Can also specify to use a monotonic interpolation for |B| rather
        than a cubic Hermite spline with ``monotonic=True``.

    Returns
    -------
    bounce_integrate : callable
        This callable method computes the bounce integral ∫ f(ℓ) dℓ for every
        specified field line ℓ for every λ value in ``pitch``.
    items : dict
        grid_desc : Grid
            DESC coordinate grid for the given field line coordinates.
        grid_fl : Grid
            Clebsch-Type field-line coordinate grid.
        knots : Array,
            Field line-following ζ coordinates of spline knots.
        B.c : Array, shape(4, S, knots.size - 1)
            Polynomial coefficients of the spline of |B| in local power basis.
            First axis enumerates the coefficients of power series.
            Second axis enumerates the splines along the field lines.
            Last axis enumerates the polynomials of the spline along a particular
            field line.
        B_z_ra.c : Array, shape(3, S, knots.size - 1)
            Polynomial coefficients of the spline of ∂|B|/∂_ζ in local power basis.
            First axis enumerates the coefficients of power series.
            Second axis enumerates the splines along the field lines.
            Last axis enumerates the polynomials of the spline along a particular
            field line.

    Examples
    --------
    Suppose we want to compute a bounce average of the function
    f(ℓ) = (1 − λ |B|) * g_zz, where g_zz is the squared norm of the
    toroidal basis vector on some set of field lines specified by (ρ, α)
    coordinates. This is defined as
        (∫ f(ℓ) / √(1 − λ |B|) dℓ) / (∫ 1 / √(1 − λ |B|) dℓ)


    .. code-block:: python

        def integrand_num(g_zz, B, pitch, Z):
            # Integrand in integral in numerator of bounce average.
            f = (1 - pitch * B) * g_zz
            return safediv(f, jnp.sqrt(1 - pitch * B))

        def integrand_den(B, pitch, Z):
            # Integrand in integral in denominator of bounce average.
            return safediv(1, jnp.sqrt(1 - pitch * B))

        eq = get("HELIOTRON")
        rho = jnp.linspace(1e-12, 1, 6)
        alpha = jnp.linspace(0, (2 - eq.sym) * jnp.pi, 5)
        bounce_integrate, items = bounce_integral(eq, rho, alpha)

        g_zz = eq.compute("g_zz", grid=items["grid_desc"])["g_zz"]
        pitch = pitch_of_extrema(items["knots"], items["B.c"], items["B_z_ra.c"])
        num = bounce_integrate(integrand_num, g_zz, pitch)
        den = bounce_integrate(integrand_den, [], pitch)
        average = num / den
        assert jnp.isfinite(average).any()

        # Now we can group the data by field line.
        average = average.reshape(pitch.shape[0], rho.size, alpha.size, -1)
        # The bounce averages stored at index i, j
        i, j = 0, 0
        print(average[:, i, j])
        # are the bounce averages along the field line with nodes
        # given in Clebsch-Type field-line coordinates ρ, α, ζ
        nodes = items["grid_fl"].nodes.reshape(rho.size, alpha.size, -1, 3)
        print(nodes[i, j])
        # for the pitch values stored in
        pitch = pitch.reshape(pitch.shape[0], rho.size, alpha.size)
        print(pitch[:, i, j])
        # Some of these bounce averages will evaluate as nan.
        # You should filter out these nan values when computing stuff.
        print(jnp.nansum(average, axis=-1))

    """
    monotonic = kwargs.pop("monotonic", False)
    if quad == tanh_sinh_quad:
        kwargs.setdefault("resolution", 19)
    x, w = quad(**kwargs)
    # The gradient of the transformation is the weight function w(x) of the integral.
    auto, grad_auto = automorphism
    w = w * grad_auto(x)
    # Recall x = auto_forward(_affine_bijection_forward(ζ, ζ_b₁, ζ_b₂)).
    # Apply reverse automorphism to quadrature points.
    x = auto(x)

    if alpha is None:
        alpha = jnp.linspace(0, (2 - eq.sym) * jnp.pi, 10)
    rho = jnp.atleast_1d(rho)
    alpha = jnp.atleast_1d(alpha)
    knots = jnp.atleast_1d(knots)
    # number of field lines or splines
    S = rho.size * alpha.size

    # Compute |B| and group data along field lines.
    grid_desc, grid_fl = desc_grid_from_field_line_coords(eq, rho, alpha, knots)
    data = eq.compute(
        ["B^zeta", "|B|", "|B|_z|r,a"],
        grid=grid_desc,
        # TODO: look into override grid in different PR
        override_grid=False,
    )
    B_sup_z = data["B^zeta"].reshape(S, knots.size) * L_ref / B_ref
    B = data["|B|"].reshape(S, knots.size) / B_ref
    B_z_ra = data["|B|_z|r,a"].reshape(S, knots.size) / B_ref
    # Compute spline of |B| along field lines.
    B_c = (
        PchipInterpolator(knots, B, axis=-1, check=check).c
        if monotonic
        else CubicHermiteSpline(knots, B, B_z_ra, axis=-1, check=check).c
    )
    B_c = jnp.moveaxis(B_c, source=1, destination=-1)
    B_z_ra_c = _poly_der(B_c)
    assert B_c.shape == (4, S, knots.size - 1)
    assert B_z_ra_c.shape == (3, S, knots.size - 1)
    items = {
        "grid_desc": grid_desc,
        "grid_fl": grid_fl,
        "knots": knots,
        "B.c": B_c,
        "B_z_ra.c": B_z_ra_c,
    }

    def bounce_integrate(integrand, f, pitch, method="akima"):
        """Bounce integrate ∫ f(ℓ) dℓ.

        Parameters
        ----------
        integrand : callable
            This callable is the composition operator on the set of functions in ``f``
            that maps the functions in ``f`` to the integrand f(ℓ) in ∫ f(ℓ) dℓ.
            It should accept the items in ``f`` as arguments as well as two additional
            keyword arguments: ``B``, ``pitch``, and ``Z``, where ``Z`` is the set of
            quadrature points. A quadrature will be performed to approximate the
            bounce integral of ``integrand(*f, B=B, pitch=pitch, Z=Z)``.
            Note that any arrays baked into the callable method should broadcast
            with ``Z``.
        f : list of Array, shape(P, items["grid_desc"].num_nodes, )
            Arguments to the callable ``integrand``.
            These should be the functions in the integrand of the bounce integral
            evaluated (or interpolated to) the nodes of the returned desc
            coordinate grid.
            Should have at most two dimensions, in which case the first axis
            is interpreted as the batch axis, which enumerates the evaluation
            of the function at particular pitch values.
        pitch : Array, shape(P, S)
            λ values to evaluate the bounce integral at each field line.
            λ(ρ, α) is specified by ``pitch[..., (ρ, α)]``
            where in the latter the labels (ρ, α) are interpreted as index into the
            last axis that corresponds to that field line.
            If two-dimensional, the first axis is the batch axis as usual.
        method : str
            Method of interpolation for functions contained in ``f``.
            Defaults to akima spline to suppress oscillation.
            See https://interpax.readthedocs.io/en/latest/_api/interpax.interp1d.html.

        Returns
        -------
        result : Array, shape(P, S, (knots.size - 1) * 3)
            First axis enumerates pitch values. Second axis enumerates the field
            lines. Last axis enumerates the bounce integrals.

        """
        bp1, bp2 = bounce_points(pitch, knots, B_c, B_z_ra_c, check, plot)
        result = _bounce_quadrature(
            bp1,
            bp2,
            x,
            w,
            integrand,
            f,
            B_sup_z,
            B,
            B_z_ra,
            pitch,
            knots,
            method,
            method_B="monotonic" if monotonic else "cubic",
            check=check,
        )
        assert result.shape[-1] == (knots.size - 1) * 3
        return result

    return bounce_integrate, items