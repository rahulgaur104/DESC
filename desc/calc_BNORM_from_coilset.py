"""Find BNORMAL on surfac given a coilset and plot it."""
import os

import jax.numpy as np
import matplotlib.pyplot as plt

from desc.equilibrium import EquilibriaFamily, Equilibrium
from desc.grid import LinearGrid
from desc.io import load
from desc.magnetic_fields import SumMagneticField, ToroidalMagneticField, _MagneticField


def calc_BNORM_from_coilset(
    coils, eqname, alpha, step, external_field=None, save=True, **kwargs
):
    """Find BNORMAL on surfac given a coilset and plot it, and save to a txt file.

    Parameters
    ----------
    coils : _MagneticField
        A DESC _MagneticField object (which can be a CoilSet) to find the Bnormal from
    eqname : str or Equilibrium
        The DESC equilibrum the surface current potential was found for
        If str, assumes it is the name of the equilibrium .h5 output and will
        load it
    alpha : float
        regularization parameter used in run_regcoil
        #TODO: can remove this and replace with something like
        basename to be used for every saved figure
    step : int, optional
        Amount of points to step when saving the coil geometry
        by default 2, meaning that every other point will be saved
        if higher, less points will be saved
        #TODO: can remove this and replace with something like
        basename to be used for every saved figure
    external_field : _MagneticField, optional
        Magnetic field external to the coils given, to
        include when field line tracing.
        for example, a simple TF field
        should be same field as that used when calling
        run_regcoil to find the surface current that was
        discretized into the coilset
    save : Bool
        if True, saves figs and text file of info

    Returns
    -------
    axis_B_ratio : float
        ratio of B from eq to B from coilset.
        Should be close to 1 for a good coilset


    """
    plt.rcParams.update({"font.size": 24})
    if isinstance(eqname, str):
        eq = load(eqname)
    elif isinstance(eqname, EquilibriaFamily) or isinstance(eqname, Equilibrium):
        eq = eqname
    if hasattr(eq, "__len__"):
        eq = eq[-1]
    B0 = kwargs.get("B0", None)
    if B0:
        # TODO: go thru scripts and change to use the external_field argument,
        #  and remove this
        external_field = B0  # support old way of doing it
    if external_field:
        R0_ves = 0.7035
        if not isinstance(external_field, _MagneticField):
            assert (
                float(external_field) == external_field
            ), "external_field must be a float or a _MagneticField!"
            external_field = ToroidalMagneticField(
                R0=R0_ves, external_field=external_field
            )

        coils = SumMagneticField(coils, external_field)

    dirname = f"{eqname.split('/')[-1].strip('.h5')}"
    if not os.path.isdir(dirname):
        os.mkdir(dirname)

    grid = LinearGrid(rho=1, M=40, N=40, axis=False, NFP=eq.NFP, sym=False)
    grid_ax = LinearGrid(
        rho=np.array(1e-6), theta=np.array(0.0), N=10, axis=False, NFP=eq.NFP, sym=False
    )

    data_desc = eq.compute(["|B|", "R", "phi", "Z"], grid=grid)
    data_ax = eq.compute(["|B|", "R", "phi", "Z"], grid=grid_ax)

    cords = np.vstack((data_desc["R"], data_desc["phi"], data_desc["Z"])).T
    cords_ax = np.vstack((data_ax["R"], data_ax["phi"], data_ax["Z"])).T

    B = coils.compute_magnetic_field(cords, basis="rpz")
    B_ax = coils.compute_magnetic_field(cords_ax, basis="rpz")

    Bnorm, _ = coils.compute_Bnormal(eq.surface, grid)

    axis_B_ratio = np.mean(np.abs(data_ax["|B|"])) / np.mean(
        np.linalg.norm(B_ax, axis=-1)
    )

    print(f"Maximum |Bnormal| on surface: {np.max(np.abs(Bnorm))}")
    print(f"Minimum |Bnormal| on surface: {np.min(np.abs(Bnorm))}")
    print(f"average |Bnormal| on surface: {np.mean(np.abs(Bnorm))}")
    print(f"average |B| on surface: {np.mean(np.abs(np.linalg.norm(B,axis=-1)))}\n")
    print(f"average |B| on axis: {np.mean(np.abs(np.linalg.norm(B_ax,axis=-1)))}\n")
    print(f"eq average |B| on surface: {np.mean(np.abs(data_desc['|B|']))}\n")
    print(f"eq average |B| on axis: {np.mean(np.abs(data_ax['|B|']))}\n")
    print("|B| on axis eq / |B| on axis coil :" f"{axis_B_ratio}\n")

    plt.rcParams.update({"font.size": 30})
    plt.rcParams.update({"ytick.labelsize": 22})
    plt.rcParams.update({"xtick.labelsize": 22})

    plt.figure(figsize=(8, 8))
    plt.contourf(
        grid.nodes[grid.unique_theta_idx, 1],
        grid.nodes[grid.unique_zeta_idx, 2],
        Bnorm.reshape(grid.num_theta, grid.num_zeta),
    )
    plt.colorbar()
    plt.xlabel(r"$\theta$")
    plt.ylabel(r"$\zeta$")

    plt.title(kwargs.get("title", "Bnormal from coilset"))

    if save:
        plt.savefig(
            f"{dirname}/Bnormal" f"_alpha_{alpha:1.4e}_step_{step}_{dirname}.png"
        )
        with open(
            f"{dirname}/Bnormal_info" f"_alpha_{alpha:1.4e}_step_{step}_{dirname}.txt",
            "w+",
        ) as f:
            f.write(f"Maximum |Bnormal| on surface: {np.max(np.abs(Bnorm))}\n")
            f.write(f"Minimum |Bnormal| on surface: {np.min(np.abs(Bnorm))}\n")
            f.write(f"average |Bnormal| on surface: {np.mean(np.abs(Bnorm))}\n")
            f.write(
                "average |B| on surface:"
                f"{np.mean(np.abs(np.linalg.norm(B,axis=-1)))}\n"
            )
            f.write(
                "average |B| on axis:"
                f" {np.mean(np.abs(np.linalg.norm(B_ax,axis=-1)))}\n"
            )
            f.write(
                "eq average |B| on surface: " f"{np.mean(np.abs(data_desc['|B|']))}\n"
            )
            f.write(f"eq average |B| on axis: {np.mean(np.abs(data_ax['|B|']))}\n")

    return axis_B_ratio