"""Python implementation of REGCOIL algorithm."""

import time

import jax
import jax.numpy as jnp
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from scipy.constants import mu_0

from desc.basis import DoubleFourierSeries
from desc.geometry import FourierRZToroidalSurface
from desc.geometry.utils import rpz2xyz, rpz2xyz_vec
from desc.grid import LinearGrid
from desc.io import load
from desc.magnetic_fields import ToroidalMagneticField
from desc.transform import Transform


######################### Define functions #######################
@jax.jit
def biot_loop(re, rs, J, dV):
    """Biot-savart law calculation, returns B in cartesian coordinates.

    Parameters
    ----------
    re : ndarray, shape(n_eval_pts, 3)
        evaluation points in cartesian
    rs : ndarray, shape(n_src_pts, 3)
        source points in cartesian
    J : ndarray, shape(n_src_pts, 3)
        current density vector at source points in cartesian
    dV : ndarray, shape(n_src_pts)
        volume element at source points
    """
    re, rs, J, dV = map(jnp.asarray, (re, rs, J, dV))
    assert J.shape == rs.shape
    JdV = J * dV[:, None]
    B = jnp.zeros_like(re)

    def body(i, B):
        r = re - rs[i, :]
        num = jnp.cross(JdV[i, :], r, axis=-1)
        den = jnp.linalg.norm(r, axis=-1) ** 3
        B = B + jnp.where(den[:, None] == 0, 0, num / den[:, None])
        return B

    return 1e-7 * jax.lax.fori_loop(0, J.shape[0], body, B)


def run_regcoil(  # noqa: C901 fxn too complex
    eqname,
    helicity_ratio=0,
    alpha=0,
    basis_M=16,
    basis_N=16,
    grid_M=15,
    grid_N=None,
    winding_surf=None,
    scan=False,
    nscan=30,
    scan_lower=-30,
    scan_upper=-1,
    external_TF_fraction=0,
):
    """Python regcoil to find single-valued current.

    Parameters
    ----------
    eqname: str, name of DESC eq to calculate the current on the
        winding surface which makes Bnormal=0 on LCFS
    helicity_ratio: int, used to determine if coils are modular (0) or helical (!=0)
    alpha: float, regularization parameter, >0, regularizes minimization of Bn
        with minimization of K on winding surface
        i.e. larger alpha, simpler coilset and smaller currents, but worse Bn
    basis_M: int, poloidal resolution of Single valued partof current potential
    basis_N: int, Toroidal resolution of Single valued partof current potential
    grid_M: int, poloidal resolution of source and eval grids
    grid_N: int, Toroidal resolution of source and eval grids, defaults to grid_M*NFP
    winding_surf: FourierRZToroidalSurface, surface to find current potential on.
         If None, defaults to NT_tao circular torus
    scan: bool, whether to scan over alpha values starting from 0
        and ending at the given alpha and return a plot of the chiB vs alpha
    nscan: int, number of alpha values to scan over
    scan_lower: int, default -30, power of 10 (i.e. 10^(-30)) that is the
        lower bound of the alpha values to scan over
    scan_upper: int, default -1, power of 10 (i.e. 10^(-1)) that is the
        upper bound of the alpha values to scan over
    external_TF_fraction: float, default 0
        how much TF is provided by coils external to the
        winding surface being considered.

    Returns
    -------
    phi_mn_opt: array, the double fourier series coefficients for the
         single-valued part of the current potential
    curr_pot_trans: Transform, transform for the basis used for the phi_mn_opt,
         can find value of phi_SV with curr_pot_trans.transform(phi_mn_opt)
    I: float, net toroidal current linking the plasma and coils,
         determined by helicity ratio and G
    G: float, net poloidal current linking the plasma and coils,
         determined by the equilibrium toroidal field
         note: this value is the value after subtraction of the
         external linking poloidal current if external_TF_fraction > 0
    phi_total_function: fxn, accepts a LinearGrid object (or any grid),
         and returns the total current potential on that grid. Convenience function.
    TF_B: ToroidalMagneticField, the TF provided by external TF coils.
    """
    ##### Load in DESC equilbrium #####
    eqfv = load(eqname)
    eq = eqfv

    ############### calculate quantities on DESC  plasma surface ###############
    if grid_N is None:
        grid_N = basis_N * eq.NFP
    sgrid = LinearGrid(M=grid_M, N=grid_N)  # source must be for all NFP still
    egrid = sgrid
    edata = eq.compute(["e^rho", "R", "Z", "phi", "e_theta", "e_zeta"], egrid)

    ne = jnp.cross(
        edata["e_theta"], edata["e_zeta"], axis=-1
    )  # surface normal on evaluation surface (ie plasma bdry)
    ne = rpz2xyz_vec(ne, phi=egrid.nodes[:, 2])
    ne_mag = jnp.linalg.norm(ne, axis=-1)
    ne = ne / ne_mag[:, None]
    re = jnp.array(
        [edata["R"], egrid.nodes[:, 2], edata["Z"]]
    ).T  # evaluation points on plasma bdry

    if winding_surf is None:
        # use nt tao as default
        R0_ves = 0.7035  # m
        a_ves = 0.0365  # m

        winding_surf = FourierRZToroidalSurface(
            R_lmn=np.array([R0_ves, -a_ves]),  # boundary coefficients in m
            Z_lmn=np.array([a_ves]),
            modes_R=np.array([[0, 0], [1, 0]]),  # [M, N] boundary Fourier modes
            modes_Z=np.array([[-1, 0]]),
            NFP=1,  # number of (toroidal) field periods
        )
    # make basis for current potential double fourier series
    curr_pot_basis = DoubleFourierSeries(M=basis_M, N=basis_M, NFP=eq.NFP)
    curr_pot_trans = Transform(sgrid, curr_pot_basis, derivs=1, build=True)

    # calc quantities on winding surface (source)
    rs = winding_surf.compute_coordinates(
        grid=sgrid
    )  # surface normal on winding surface
    rs_t = winding_surf.compute_coordinates(grid=sgrid, dt=1)
    rs_z = winding_surf.compute_coordinates(grid=sgrid, dz=1)
    phi_mn = jnp.zeros(curr_pot_basis.num_modes)

    # calculate net enclosed poloidal and toroidal currents
    G_tot = eq.compute("G", grid=egrid)["G"][0] / mu_0 * 2 * np.pi  # poloidal
    # 2pi factor is present in regcoil code
    #  https://github.com/landreman/regcoil/blob
    # /99f9abf8b0b0c6ec7bb6e7975dbee5e438808162/regcoil_init_plasma_mod.f90#L500
    assert (
        external_TF_fraction >= 0 and external_TF_fraction <= 1
    ), "external_TF_fraction must be a float between 0 and 1!"

    G_ext = external_TF_fraction * G_tot

    G = G_tot - G_ext

    if helicity_ratio == 0:  # modular coils
        I = 0  # toroidal
    else:
        I = G / helicity_ratio / eq.NFP  # toroidal

    # define fxns to calculate Bnormal from SV part of phi and from secular part
    def B_from_K_SV(phi_mn, I, G, re, rs, rs_t, rs_z, ne):
        """B from periodic part of K i.e. V^{SV}_{normal}{phi_sv} from REGCOIL eq 4."""
        phi_t = curr_pot_trans.transform(phi_mn, dt=1)
        phi_z = curr_pot_trans.transform(phi_mn, dz=1)
        ns_mag = np.linalg.norm(np.cross(rs_t, rs_z), axis=1)
        K = -(phi_t * (1 / ns_mag) * rs_z.T).T + (phi_z * (1 / ns_mag) * rs_t.T).T
        dV = sgrid.weights * jnp.linalg.norm(jnp.cross(rs_t, rs_z, axis=-1), axis=-1)
        B = biot_loop(
            rpz2xyz(re), rpz2xyz(rs), rpz2xyz_vec(K, phi=sgrid.nodes[:, 2]), dV
        )
        return jnp.sum(B * ne, axis=-1)

    def B_from_K_secular(I, G, re, rs, rs_t, rs_z, ne):
        """B from secular part of K, i.e. B^GI_{normal} from REGCOIL eqn 4."""
        phi_t = I / (2 * np.pi)
        phi_z = G / (2 * np.pi)
        ns_mag = np.linalg.norm(np.cross(rs_t, rs_z), axis=1)
        K = -(phi_t * (1 / ns_mag) * rs_z.T).T + (phi_z * (1 / ns_mag) * rs_t.T).T
        dV = sgrid.weights * jnp.linalg.norm(jnp.cross(rs_t, rs_z, axis=-1), axis=-1)
        B = biot_loop(
            rpz2xyz(re), rpz2xyz(rs), rpz2xyz_vec(K, phi=sgrid.nodes[:, 2]), dV
        )
        return jnp.sum(B * ne, axis=-1)

    # $B$ is linear in $K$ as long as the geometry is fixed
    #  so just need to evaluate the Jacobian
    t_start = time.time()
    print("Starting Jacobian Calculation")
    A = jax.jacfwd(B_from_K_SV)(phi_mn, I, G, re, rs, rs_t, rs_z, ne)
    print(f"Jacobian Calculation finished, took {time.time()-t_start} s")

    B_GI_normal = B_from_K_secular(I, G, re, rs, rs_t, rs_z, ne)
    Bn = np.zeros_like(B_GI_normal)
    if external_TF_fraction == 0:
        Bn_ext = np.zeros_like(B_GI_normal)
        TF_B = ToroidalMagneticField(B0=0, R0=R0_ves)
    else:
        TF_B = ToroidalMagneticField(B0=mu_0 * G_ext / 2 / jnp.pi, R0=R0_ves)
        Bn_ext = B_from_K_secular(0, G_ext, re, rs, rs_t, rs_z, ne)

    rhs = -(Bn + Bn_ext + B_GI_normal).T @ A

    # alpha is regularization param,
    # if >0, makes simpler coils (less current density), but worse Bnormal
    alphas = (
        [alpha]
        if not scan
        else jnp.concatenate(
            (jnp.array([0.0]), jnp.logspace(scan_lower, scan_upper, nscan))
        )
    )
    chi2Bs = []
    phi_mns = []

    for alpha in alphas:
        printstring = f"Calculating Phi_SV for alpha = {alpha:1.5e}"
        print(
            "#" * len(printstring) + "\n" + printstring + "\n" + "#" * len(printstring)
        )

        # calculate phi
        phi_mn_opt = jnp.linalg.pinv(A.T @ A + alpha * jnp.eye(A.shape[1])) @ rhs
        phi_mns.append(phi_mn_opt)

        Bn_SV = A @ phi_mn_opt
        Bn_tot = Bn_SV + Bn + B_GI_normal + Bn_ext
        chi_B = np.sum(Bn_tot * Bn_tot * ne_mag)
        chi2Bs.append(chi_B)
        printstring = f"chi^2 B = {chi_B:1.5e}"
        print(printstring)
        printstring = f"min Bnormal = {np.min(Bn_tot):1.5e}"
        print(printstring)
        printstring = f"Max Bnormal = {np.max(Bn_tot):1.5e}"
        print(printstring)
        printstring = f"Avg Bnormal = {np.mean(Bn_tot):1.5e}"
        print(printstring)
    lowest_idx_without_saddles = -1
    saddles_exists_bools = []
    ncontours = 20

    if scan:
        plt.figure(figsize=(10, 8))
        plt.rcParams.update({"font.size": 24})
        plt.scatter(alphas, chi2Bs, label="python regcoil")
        plt.xlabel("alpha (regularization parameter)")
        plt.ylabel(r"$\chi^2_B = \int \int B_{normal}^2 dA$ ")
        plt.yscale("log")
        plt.xscale("log")

        nlambda = len(chi2Bs)
        max_nlambda_for_contour_plots = 16
        numPlots = min(nlambda, max_nlambda_for_contour_plots)
        ilambda_to_plot = np.sort(
            list(set(map(int, np.linspace(1, nlambda, numPlots))))
        )
        numPlots = len(ilambda_to_plot)
        print("ilambda_to_plot:", ilambda_to_plot)

        numCols = int(np.ceil(np.sqrt(numPlots)))
        numRows = int(np.ceil(numPlots * 1.0 / numCols))

        mpl.rc("xtick", labelsize=7)
        mpl.rc("ytick", labelsize=7)

        ########################################################
        # Plot total current potential
        ########################################################

        plt.figure(figsize=(15, 8))

        for whichPlot in range(numPlots):
            plt.subplot(numRows, numCols, whichPlot + 1)
            phi_mn_opt = phi_mns[ilambda_to_plot[whichPlot] - 1]
            phi = curr_pot_trans.transform(phi_mn_opt)

            phi_tot = (
                phi
                + G / 2 / np.pi * curr_pot_trans.grid.nodes[:, 2]
                + I / 2 / np.pi * curr_pot_trans.grid.nodes[:, 1]
            )
            plt.rcParams.update({"font.size": 18})

            cdata = plt.contour(
                egrid.nodes[egrid.unique_zeta_idx, 2],
                egrid.nodes[egrid.unique_theta_idx, 1],
                (phi_tot).reshape(egrid.num_theta, egrid.num_zeta, order="F"),
                levels=ncontours,
            )
            plt.ylabel("theta")
            plt.xlabel("zeta")
            plt.title(
                f"lambda= {alphas[ilambda_to_plot[whichPlot] - 1]:1.5e}"
                + "f index = {ilambda_to_plot[whichPlot] - 1}",
                fontsize="x-small",
            )
            plt.colorbar()
            plt.xlim([0, 2 * np.pi / eq.NFP])
            saddles_exist_in_potential = False
            for j in range(ncontours):
                p = cdata.collections[j].get_paths()[0]
                v = p.vertices
                temp_zeta = v[:, 0]
                if np.abs(temp_zeta[-1] - temp_zeta[0]) < 1e-2:
                    saddles_exist_in_potential = True
                    break
            saddles_exists_bools.append(saddles_exist_in_potential)

        plt.tight_layout()
        plt.figtext(
            0.5,
            0.995,
            "Total current potential",
            horizontalalignment="center",
            verticalalignment="top",
            fontsize="small",
        )
        if np.any(saddles_exists_bools):
            lowest_idx_without_saddles = np.where(np.asarray(saddles_exists_bools))[0][
                0
            ]
            print(
                "Lowest alpha value without saddle coil contours in potential"
                + f" = {alphas[lowest_idx_without_saddles]:1.5e}"
            )
        else:
            print(
                "No alpha value yielded a current potential without"
                + " saddle coil contours or badly behaved contours!!"
            )

    plt.figure(figsize=(10, 10))
    plt.rcParams.update({"font.size": 26})
    plt.figure(figsize=(8, 8))
    plt.contourf(
        egrid.nodes[egrid.unique_zeta_idx, 2],
        egrid.nodes[egrid.unique_theta_idx, 1],
        (Bn_tot).reshape(egrid.num_theta, egrid.num_zeta, order="F"),
    )
    plt.ylabel("theta")
    plt.xlabel("zeta")
    plt.title("Bnormal on plasma surface")
    plt.colorbar()
    plt.xlim([0, 2 * np.pi / eq.NFP])

    phi = curr_pot_trans.transform(phi_mn_opt)
    phi_tot = (
        phi
        + G / 2 / np.pi * curr_pot_trans.grid.nodes[:, 2]
        + I / 2 / np.pi * curr_pot_trans.grid.nodes[:, 1]
    )
    plt.figure(figsize=(10, 10))
    plt.rcParams.update({"font.size": 18})
    plt.figure(figsize=(8, 8))
    plt.contourf(
        egrid.nodes[egrid.unique_zeta_idx, 2],
        egrid.nodes[egrid.unique_theta_idx, 1],
        (phi_tot).reshape(egrid.num_theta, egrid.num_zeta, order="F"),
        levels=ncontours,
    )
    plt.colorbar()
    plt.contour(
        egrid.nodes[egrid.unique_zeta_idx, 2],
        egrid.nodes[egrid.unique_theta_idx, 1],
        (phi_tot).reshape(egrid.num_theta, egrid.num_zeta, order="F"),
        levels=ncontours,
    )
    plt.ylabel("theta")
    plt.xlabel("zeta")
    plt.title("Total Current Potential on plasma surface")

    plt.xlim([0, 2 * np.pi / eq.NFP])

    def phi_total_function(grid):
        """Helper fxn to calculate the total phi given a LinearGrid."""
        trans = Transform(grid, curr_pot_basis)
        phi = trans.transform(phi_mn_opt)
        return (
            phi
            + G / 2 / np.pi * trans.grid.nodes[:, 2]
            + I / 2 / np.pi * trans.grid.nodes[:, 1]
        )

    if scan:
        return phi_mns, alphas, curr_pot_trans, I, G, phi_total_function, TF_B

    return phi_mn_opt, curr_pot_trans, I, G, phi_total_function, TF_B