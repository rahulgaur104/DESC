"""Compute functions for quasisymmetry objectives."""

import numpy as np

from desc.backend import jnp, put, sign
from desc.interpolate import interp1d

from .data_index import register_compute_fun
from .utils import cross, dot


@register_compute_fun(
    name="B_theta_mn",
    label="B_{\\theta, m, n}",
    units="T \\cdot m}",
    units_long="Tesla * meters",
    description="Fourier coefficients for covariant poloidal component of "
    + "magnetic field",
    dim=1,
    params=[],
    transforms={"B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["B_theta"],
)
def _B_theta_mn(params, transforms, profiles, data, **kwargs):
    data["B_theta_mn"] = transforms["B"].fit(data["B_theta"])
    return data


@register_compute_fun(
    name="B_zeta_mn",
    label="B_{\\zeta, m, n}",
    units="T \\cdot m}",
    units_long="Tesla * meters",
    description="Fourier coefficients for covariant toroidal component of "
    + "magnetic field",
    dim=1,
    params=[],
    transforms={"B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["B_zeta"],
)
def _B_zeta_mn(params, transforms, profiles, data, **kwargs):
    data["B_zeta_mn"] = transforms["B"].fit(data["B_zeta"])
    return data


@register_compute_fun(
    name="w_Boozer_mn",
    label="w_{Boozer,m,n}",
    units="T \\cdot m",
    units_long="Tesla * meters",
    description="RHS of eq 10 in Hirshman 1995 'Transformation from VMEC to "
    + "Boozer Coordinates'",
    dim=1,
    params=[],
    transforms={"w": [[0, 0, 0]], "B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["B_theta_mn", "B_zeta_mn"],
)
def _w_mn(params, transforms, profiles, data, **kwargs):
    w_mn = jnp.zeros((transforms["w"].basis.num_modes,))
    Bm = transforms["B"].basis.modes[:, 1]
    Bn = transforms["B"].basis.modes[:, 2]
    wm = transforms["w"].basis.modes[:, 1]
    wn = transforms["w"].basis.modes[:, 2]
    NFP = transforms["w"].basis.NFP
    # indices of matching modes in w and B bases
    # need to use np instead of jnp here as jnp.where doesn't work under jit
    # even if the args are static
    ib, iw = np.where((Bm[:, None] == -wm) * (Bn[:, None] == wn) * (wm != 0))
    jb, jw = np.where(
        (Bm[:, None] == wm) * (Bn[:, None] == -wn) * (wm == 0) * (wn != 0)
    )
    w_mn = put(w_mn, iw, sign(wn[iw]) * data["B_theta_mn"][ib] / jnp.abs(wm[iw]))
    w_mn = put(w_mn, jw, sign(wm[jw]) * data["B_zeta_mn"][jb] / jnp.abs(NFP * wn[jw]))
    data["w_Boozer_mn"] = w_mn
    return data


@register_compute_fun(
    name="w_Boozer",
    label="w_{Boozer}",
    units="T \\cdot m",
    units_long="Tesla * meters",
    description="Inverse Fourier transform of RHS of eq 10 in Hirshman 1995 "
    + "'Transformation from VMEC to Boozer Coordinates'",
    dim=1,
    params=[],
    transforms={"w": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer_mn"],
)
def _w(params, transforms, profiles, data, **kwargs):
    data["w_Boozer"] = transforms["w"].transform(data["w_Boozer_mn"])
    return data


@register_compute_fun(
    name="w_Boozer_t",
    label="\\partial_{\\theta} w_{Boozer}",
    units="T \\cdot m",
    units_long="Tesla * meters",
    description="Inverse Fourier transform of RHS of eq 10 in Hirshman 1995 "
    + "'Transformation from VMEC to Boozer Coordinates', poloidal derivative",
    dim=1,
    params=[],
    transforms={"w": [[0, 1, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer_mn"],
)
def _w_t(params, transforms, profiles, data, **kwargs):
    data["w_Boozer_t"] = transforms["w"].transform(data["w_Boozer_mn"], dt=1)
    return data


@register_compute_fun(
    name="w_Boozer_z",
    label="\\partial_{\\zeta} w_{Boozer}",
    units="T \\cdot m",
    units_long="Tesla * meters",
    description="Inverse Fourier transform of RHS of eq 10 in Hirshman 1995 "
    + "'Transformation from VMEC to Boozer Coordinates', toroidal derivative",
    dim=1,
    params=[],
    transforms={"w": [[0, 0, 1]]},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer_mn"],
)
def _w_z(params, transforms, profiles, data, **kwargs):
    data["w_Boozer_z"] = transforms["w"].transform(data["w_Boozer_mn"], dz=1)
    return data


@register_compute_fun(
    name="nu",
    label="\\nu = \\zeta_{B} - \\zeta",
    units="rad",
    units_long="radians",
    description="Boozer toroidal stream function",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer", "G", "I", "iota", "lambda"],
)
def _nu(params, transforms, profiles, data, **kwargs):
    GI = data["G"] + data["iota"] * data["I"]
    data["nu"] = (data["w_Boozer"] - data["I"] * data["lambda"]) / GI
    return data


@register_compute_fun(
    name="nu_t",
    label="\\partial_{\\theta} \\nu",
    units="rad",
    units_long="radians",
    description="Boozer toroidal stream function, derivative wrt poloidal angle",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer_t", "G", "I", "iota", "lambda_t"],
)
def _nu_t(params, transforms, profiles, data, **kwargs):
    GI = data["G"] + data["iota"] * data["I"]
    data["nu_t"] = (data["w_Boozer_t"] - data["I"] * data["lambda_t"]) / GI
    return data


@register_compute_fun(
    name="nu_z",
    label="\\partial_{\\zeta} \\nu",
    units="rad",
    units_long="radians",
    description="Boozer toroidal stream function, derivative wrt toroidal angle",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["w_Boozer_z", "G", "I", "iota", "lambda_z"],
)
def _nu_z(params, transforms, profiles, data, **kwargs):
    GI = data["G"] + data["iota"] * data["I"]
    data["nu_z"] = (data["w_Boozer_z"] - data["I"] * data["lambda_z"]) / GI
    return data


@register_compute_fun(
    name="theta_B",
    label="\\theta_{B}",
    units="rad",
    units_long="radians",
    description="Boozer poloidal angular coordinate",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["theta", "lambda", "iota", "nu"],
)
def _theta_B(params, transforms, profiles, data, **kwargs):
    data["theta_B"] = data["theta"] + data["lambda"] + data["iota"] * data["nu"]
    return data


@register_compute_fun(
    name="zeta_B",
    label="\\zeta_{B}",
    units="rad",
    units_long="radians",
    description="Boozer toroidal angular coordinate",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["zeta", "nu"],
)
def _zeta_B(params, transforms, profiles, data, **kwargs):
    data["zeta_B"] = data["zeta"] + data["nu"]
    return data


@register_compute_fun(
    name="sqrt(g)_B",
    label="\\sqrt{g}_{B}",
    units="~",
    units_long="None",
    description="Jacobian determinant of Boozer coordinates",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["lambda_t", "lambda_z", "nu_t", "nu_z", "iota"],
)
def _sqrtg_B(params, transforms, profiles, data, **kwargs):
    data["sqrt(g)_B"] = (1 + data["lambda_t"]) * (1 + data["nu_z"]) + (
        data["iota"] - data["lambda_z"]
    ) * data["nu_t"]
    return data


@register_compute_fun(
    name="|B|_mn",
    label="B_{mn}^{Boozer}",
    units="T",
    units_long="Tesla",
    description="Boozer harmonics of magnetic field",
    dim=1,
    params=[],
    transforms={"B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["sqrt(g)_B", "|B|", "rho", "theta_B", "zeta_B"],
)
def _B_mn(params, transforms, profiles, data, **kwargs):
    nodes = jnp.array([data["rho"], data["theta_B"], data["zeta_B"]]).T
    norm = 2 ** (3 - jnp.sum((transforms["B"].basis.modes == 0), axis=1))
    data["|B|_mn"] = (
        norm  # 1 if m=n=0, 2 if m=0 or n=0, 4 if m!=0 and n!=0
        * jnp.matmul(
            transforms["B"].basis.evaluate(nodes).T, data["sqrt(g)_B"] * data["|B|"]
        )
        / transforms["B"].grid.num_nodes
    )
    return data


@register_compute_fun(
    name="B modes",
    label="Boozer modes",
    units="~",
    units_long="None",
    description="Boozer harmonics",
    dim=1,
    params=[],
    transforms={"B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=[],
)
def _B_modes(params, transforms, profiles, data, **kwargs):
    data["B modes"] = transforms["B"].basis.modes
    return data


@register_compute_fun(
    name="f_C",
    label="(M \\iota - N) (\\mathbf{B} \\times \\nabla \\psi) \\cdot \\nabla B"
    + " - (M G + N I) \\mathbf{B} \\cdot \\nabla B",
    units="T^{3}",
    units_long="Tesla cubed",
    description="Two-term quasisymmetry metric",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=[
        "iota",
        "psi_r",
        "sqrt(g)",
        "B_theta",
        "B_zeta",
        "|B|_t",
        "|B|_z",
        "G",
        "I",
        "B*grad(|B|)",
    ],
    helicity="helicity",
)
def _f_C(params, transforms, profiles, data, **kwargs):
    M, N = kwargs.get("helicity", (1, 0))
    data["f_C"] = (M * data["iota"] - N) * (data["psi_r"] / data["sqrt(g)"]) * (
        data["B_zeta"] * data["|B|_t"] - data["B_theta"] * data["|B|_z"]
    ) - (M * data["G"] + N * data["I"]) * data["B*grad(|B|)"]
    return data


@register_compute_fun(
    name="f_T",
    label="\\nabla \\psi \\times \\nabla B \\cdot \\nabla "
    + "(\\mathbf{B} \\cdot \\nabla B)",
    units="T^{4} \\cdot m^{-2}",
    units_long="Tesla quarted / square meters",
    description="Triple product quasisymmetry metric",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=[
        "psi_r",
        "sqrt(g)",
        "|B|_t",
        "|B|_z",
        "(B*grad(|B|))_t",
        "(B*grad(|B|))_z",
    ],
)
def _f_T(params, transforms, profiles, data, **kwargs):
    data["f_T"] = (data["psi_r"] / data["sqrt(g)"]) * (
        data["|B|_t"] * data["(B*grad(|B|))_z"]
        - data["|B|_z"] * data["(B*grad(|B|))_t"]
    )
    return data


@register_compute_fun(
    name="eta",
    label="\\eta",
    units="rad",
    units_long="radians",
    description="Intermediate omnigenity coordinate along field lines",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["theta"],
)
def _eta(params, transforms, profiles, data, **kwargs):
    # theta is used as a placeholder for eta (angle along field lines)
    data["eta"] = (data["theta"] - jnp.pi) / 2
    return data


@register_compute_fun(
    name="h",
    label="h = \\theta + (N / M) \\zeta",
    units="rad",
    units_long="radians",
    description="Omnigenity symmetry angle",
    dim=1,
    params=["omni_lmn"],
    transforms={"omni": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["rho", "zeta", "eta"],
)
def _omni_angle(params, transforms, profiles, data, **kwargs):
    alpha = data["zeta"]  # zeta is used as a placeholder for alpha (field line label)
    nodes = jnp.array([data["rho"], data["eta"], alpha]).T

    data["h"] = (
        transforms["omni"].basis.evaluate(nodes) @ params["omni_lmn"]
        + 2 * data["eta"]
        + jnp.pi
    )
    return data


@register_compute_fun(
    name="|B|_omni",
    label="|\\mathbf{B}_{omni}|",
    units="T",
    units_long="Tesla",
    description="Magnitude of omnigeneous magnetic field",
    dim=1,
    params=["well_l"],
    transforms={"well": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["eta"],
)
def _B_well(params, transforms, profiles, data, **kwargs):
    # reshaped to size (L_well, M_well)
    well_arr = params["well_l"].reshape((transforms["well"].basis.L + 1, -1))
    # assuming single flux surface, so only take first row (single node)
    B_input = (transforms["well"].matrices["direct1"][0][0][0] @ well_arr)[0, :]
    B_input = jnp.sort(B_input)  # sort to ensure monotonicity
    eta_input = jnp.linspace(0, jnp.pi / 2, num=B_input.size)

    # |B|_omnigeneous is an even function so B(-eta) = B(+eta) = B(|eta|)
    data["|B|_omni"] = interp1d(
        jnp.abs(data["eta"]), eta_input, B_input, method="monotonic-0"
    )
    return data


@register_compute_fun(
    name="|B|(eta,alpha)",
    label="|\\mathbf{B}|(\\eta,\\alpha)",
    units="T",
    units_long="Tesla",
    description="Magnitude of magnetic field at (eta,alpha) coordinates",
    dim=1,
    params=[],
    transforms={"B": [[0, 0, 0]]},
    profiles=[],
    coordinates="rtz",
    data=["zeta", "h", "iota", "|B|_mn"],
    helicity="helicity",
)
def _B_omni_coords(params, transforms, profiles, data, **kwargs):
    M, N = kwargs.get("helicity", (0, 1))
    iota = data["iota"][0]  # FIXME: assumes a single flux surface
    if M == 0:
        matrix = jnp.array([[N - M * iota, iota / N], [0, 1 / N]])
    else:
        matrix = jnp.array([[N, M * iota / (N - M * iota)], [M, M / (N - M * iota)]])
    alpha = data["zeta"]  # zeta is used as a placeholder for alpha (field line label)

    # solve for (theta_B,zeta_B) cooresponding to (eta,alpha)
    booz = matrix @ jnp.vstack((alpha, data["h"]))
    data["theta_B(eta,alpha)"] = booz[0, :]
    data["zeta_B(eta,alpha)"] = booz[1, :]

    nodes = jnp.vstack((data["rho"], booz)).T
    data["|B|(eta,alpha)"] = jnp.matmul(
        transforms["B"].basis.evaluate(nodes), data["|B|_mn"]
    )
    return data


@register_compute_fun(
    name="omnigenity",
    label="f_{omnigenity}",
    units="T",
    units_long="Tesla",
    description="Omnigenity error",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["|B|_omni", "|B|(eta,alpha)"],
)
def _omnigenity(params, transforms, profiles, data, **kwargs):
    data["omnigenity"] = data["|B|(eta,alpha)"] - data["|B|_omni"]
    return data


@register_compute_fun(
    name="isodynamicity",
    label="1/B^2 (\\mathbf{b} \\times \\nabla B) \\cdot \\nabla \\psi",
    units="~",
    units_long="None",
    description="Measure of cross field drift at each point, "
    + "unweighted by particle energy",
    dim=1,
    params=[],
    transforms={},
    profiles=[],
    coordinates="rtz",
    data=["b", "grad(|B|)", "|B|", "grad(psi)"],
)
def _isodynamicity(params, transforms, profiles, data, **kwargs):
    data["isodynamicity"] = (
        dot(cross(data["b"], data["grad(|B|)"]), data["grad(psi)"]) / data["|B|"] ** 2
    )
    return data