import numpy as np
import subprocess
from scipy.interpolate import interp1d
from scipy.constants import mu_0
import os
import time
import netCDF4 as nc

from jax import core
from jax.interpreters import ad, batching

from desc.backend import jnp
from desc.utils import Timer
from desc.derivatives import FiniteDiffDerivative
from desc.grid import Grid, LinearGrid, QuadratureGrid
from desc.transform import Transform
from desc.compute._core import dot
from desc.compute import (
    data_index,
    compute_toroidal_flux,
    compute_pressure,
    compute_rotational_transform,
    compute_lambda,
    compute_geometry,
    compute_contravariant_metric_coefficients,
    compute_covariant_magnetic_field,
    compute_magnetic_field_magnitude,
)
from .objective_funs import _Objective


class GXWrapper(_Objective):

    _scalar = True
    _linear = False

    def __init__(
        self,
        eq=None,
        target=0,
        weight=1,
        grid=None,
        name="GX",
        npol=1,
        nzgrid=32,
        alpha=0,
        psi=0.5,
        equality=True,
        lb=False,
    ):
        self.eq = eq
        self.npol = npol
        self.nzgrid = nzgrid
        self.alpha = alpha
        self.psi = psi

        self.grid = grid
        super().__init__(eq=eq, target=target, weight=weight, name=name)
        units = "Q"
        self._callback_fmt = "Total heat flux: {:10.3e} " + units
        self.lb = lb
        self.equality = equality

    def build(self, eq, use_jit=False, verbose=1):
        if self.grid is None:
            self.grid_eq = QuadratureGrid(
                L=eq.L_grid,
                M=eq.M_grid,
                N=eq.N_grid,
                NFP=eq.NFP,
                # sym=eq.sym,
                # axis=False,
                # rotation="sin",
                # node_pattern=eq.node_pattern,
            )

            grid_1d = LinearGrid(L=500, theta=0, zeta=0)
            data = eq.compute("iota", grid=grid_1d)
            iotad = data["iota"]
            fi = interp1d(grid_1d.nodes[:, 0], iotad)

            # get coordinate system
            rho = np.sqrt(self.psi)
            iota = fi(rho)
            zeta = np.linspace(
                -np.pi * self.npol / iota, np.pi * self.npol / iota, 2 * self.nzgrid + 1
            )
            thetas = self.alpha * np.ones(len(zeta)) + iota * zeta

            rhoa = rho * np.ones(len(zeta))
            c = np.vstack([rhoa, thetas, zeta]).T
            coords = eq.compute_theta_coords(c)
            self.grid = Grid(coords)

        self._dim_f = 1

        timer = Timer()
        if verbose > 0:
            print("Precomputing transforms")
        timer.start("Precomputing transforms")

        self._pressure = eq.pressure.copy()
        self._iota = eq.iota.copy()
        self._iota_eq = eq.iota.copy()
        self._pressure.grid = self.grid
        self._iota.grid = self.grid
        self._iota_eq.grid = self.grid_eq

        self._R_transform = Transform(self.grid, eq.R_basis, derivs=3, build=True)
        self._Z_transform = Transform(self.grid, eq.Z_basis, derivs=3, build=True)
        self._L_transform = Transform(self.grid, eq.L_basis, derivs=3, build=True)

        self._R_transform_eq = Transform(
            self.grid_eq, eq.R_basis, derivs=data_index["V"]["R_derivs"], build=True
        )
        self._Z_transform_eq = Transform(
            self.grid_eq, eq.Z_basis, derivs=data_index["V"]["R_derivs"], build=True
        )

        self._args = ["R_lmn", "Z_lmn", "L_lmn", "i_l", "p_l", "Psi"]

        self.gx_compute = core.Primitive("gx")
        self.gx_compute.def_impl(self.compute_impl)
        ad.primitive_jvps[self.gx_compute] = self.compute_gx_jvp
        batching.primitive_batchers[self.gx_compute] = self.compute_gx_batch

        timer.stop("Precomputing transforms")
        if verbose > 1:
            timer.disp("Precomputing transforms")

        self._check_dimensions()
        self._set_dimensions(eq)
        # self._set_derivatives(use_jit=use_jit)
        self._built = True

    def compute(self, R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi):
        # print("At the beginning of compute: " + str(R_lmn[0]) + " " +str(Z_lmn[0]) + " " + str(L_lmn[0]))
        args = (R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi)
        # self.gx_compute.bind(R_lmn,Z_lmn,L_lmn,i_l,p_l,Psi)
        return self.gx_compute.bind(*args)

    def compute_impl(self, *args):
        (R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi) = args
        # grid_1d = LinearGrid(L = 500, theta=0, zeta=0)
        data = compute_rotational_transform(i_l, self._iota)
        iota = data["iota"]
        # iotad = data['iota']
        # fi = interp1d(grid_1d.nodes[:,0],iotad)

        # get coordinate system
        # rho = np.sqrt(self.psi)
        # iota = fi(rho)

        # fi = interp1d(grid_1d.nodes[:,0],iota)

        data = compute_toroidal_flux(Psi, self._iota_eq, data=data)
        psib = data["psi"][-1]
        if psib < 0:
            sgn = False
            psib = np.abs(psib)
        else:
            sgn = True

        # get coordinate system
        rho = np.sqrt(self.psi)
        # iota = fi(rho)
        iotas = iota[0]
        zeta = np.linspace(
            -np.pi * self.npol / iotas, np.pi * self.npol / iotas, 2 * self.nzgrid + 1
        )
        # thetas = self.alpha*np.ones(len(zeta)) + iota*zeta

        # rhoa = rho*np.ones(len(zeta))
        # c = np.vstack([rhoa,thetas,zeta]).T
        # coords = self.eq.compute_theta_coords(c,L_lmn=L_lmn)

        # normalizations
        # grid = Grid(coords)
        data_eq = compute_geometry(
            R_lmn, Z_lmn, self._R_transform_eq, self._Z_transform_eq
        )
        Lref = data_eq["a"]
        Bref = 2 * psib / Lref ** 2
        # print('psib is ' + str(psib))
        # print("Bref is " + str(Bref))

        # calculate bmag
        data = compute_magnetic_field_magnitude(
            R_lmn,
            Z_lmn,
            L_lmn,
            i_l,
            Psi,
            self._R_transform,
            self._Z_transform,
            self._L_transform,
            self._iota,
            data=data,
        )
        modB = data["|B|"]
        # print("modB is " + str(modB))
        bmag = modB / Bref

        # calculate gradpar and grho
        gradpar = Lref * data["B^zeta"] / modB
        data = compute_contravariant_metric_coefficients(
            R_lmn, Z_lmn, self._R_transform, self._Z_transform, data=data
        )
        grho = data["|grad(rho)|"] * Lref

        # calculate grad_psi and grad_alpha
        grad_psi = 2 * psib * rho
        data = compute_lambda(L_lmn, self._L_transform, data=data)

        lmbda = data["lambda"]
        lmbda_r = data["lambda_r"]
        lmbda_t = data["lambda_t"]
        lmbda_z = data["lambda_z"]
        # iota_data = self.eq.compute('iota')
        shear = data["iota_r"]

        grad_alpha_r = lmbda_r - zeta * shear
        grad_alpha_t = 1 + lmbda_t
        grad_alpha_z = -iota + lmbda_z

        grad_alpha = np.sqrt(
            grad_alpha_r ** 2 * data["g^rr"]
            + grad_alpha_t ** 2 * data["g^tt"]
            + grad_alpha_z ** 2 * data["g^zz"]
            + 2 * grad_alpha_r * grad_alpha_t * data["g^rt"]
            + 2 * grad_alpha_r * grad_alpha_z * data["g^rz"]
            + 2 * grad_alpha_t * grad_alpha_z * data["g^tz"]
        )

        grad_psi_dot_grad_alpha = (
            grad_psi * grad_alpha_r * data["g^rr"]
            + grad_psi * grad_alpha_t * data["g^rt"]
            + grad_psi * grad_alpha_z * data["g^rz"]
        )

        # calculate gds*
        x = Lref * rho
        shat = -x / iotas * shear[0] / Lref
        gds2 = grad_alpha ** 2 * Lref ** 2 * self.psi
        # gds21 with negative sign?
        gds21 = shat / Bref * grad_psi_dot_grad_alpha
        gds22 = (shat / (Lref * Bref)) ** 2 / self.psi * grad_psi ** 2 * data["g^rr"]

        # calculate gbdrift0 and cvdrift0
        data = compute_covariant_magnetic_field(
            R_lmn,
            Z_lmn,
            L_lmn,
            i_l,
            Psi,
            self._R_transform,
            self._Z_transform,
            self._L_transform,
            self._iota,
            data=data,
        )
        B_t = data["B_theta"]
        B_z = data["B_zeta"]
        dB_t = data["|B|_t"]
        dB_z = data["|B|_z"]
        jac = data["sqrt(g)"]
        # gbdrift0 = (B_t*dB_z - B_z*dB_t)*2*rho*psib/jac
        # gbdrift0 with negative sign?
        gbdrift0 = (
            shat
            * 2
            / modB ** 3
            / rho
            * (B_t * dB_z + B_z * dB_t)
            * psib
            / jac
            * 2
            * rho
        )
        cvdrift0 = gbdrift0

        # calculate gbdrift and cvdrift
        B_r = data["B_rho"]
        # dB_r = self.eq.compute('|B|_r',grid=grid)['|B|_r']

        # data = self.eq.compute('|B|',grid=grid)
        # data.update(self.eq.compute('B^zeta_r',grid=grid))
        # data.update(self.eq.compute('B^theta_r',grid=grid))

        data["|B|_r"] = (
            data["B^theta"]
            * (
                data["B^zeta_r"] * data["g_tz"]
                + data["B^theta_r"] * data["g_tt"]
                + data["B^theta"] * dot(data["e_theta_r"], data["e_theta"])
            )
            + data["B^zeta"]
            * (
                data["B^theta_r"] * data["g_tz"]
                + data["B^zeta_r"] * data["g_zz"]
                + data["B^zeta"] * dot(data["e_zeta_r"], data["e_zeta"])
            )
            + data["B^theta"]
            * data["B^zeta"]
            * (
                dot(data["e_theta_r"], data["e_zeta"])
                + dot(data["e_zeta_r"], data["e_theta"])
            )
        ) / data["|B|"]

        dB_r = data["|B|_r"]

        # iota = iota_data['iota'][0]
        gbdrift_norm = 2 * Bref * Lref ** 2 / modB ** 3 * rho
        gbdrift = (
            gbdrift_norm
            / jac
            * (
                B_r * dB_t * (lmbda_z - iota)
                + B_t * dB_z * (lmbda_r - zeta * shear[0])
                + B_z * dB_r * (1 + lmbda_t)
                - B_z * dB_t * (lmbda_r - zeta * shear[0])
                - B_t * dB_r * (lmbda_z - iota)
                - B_r * dB_z * (1 + lmbda_t)
            )
        )
        Bsa = 1 / jac * (B_z * (1 + lmbda_t) - B_t * (lmbda_z - iota))
        data = compute_pressure(p_l, self._pressure, data=data)
        p_r = data["p_r"]
        cvdrift = (
            gbdrift
            + 2 * Bref * Lref ** 2 / modB ** 2 * rho * mu_0 / modB ** 2 * p_r * Bsa
        )

        self.Lref = Lref
        self.shat = shat
        self.iota = iota

        self.get_gx_arrays(
            zeta,
            bmag,
            grho,
            gradpar,
            gds2,
            gds21,
            gds22,
            gbdrift,
            gbdrift0,
            cvdrift,
            cvdrift0,
            sgn,
        )
        self.write_geo()
        # self.write_input()

        self.run_gx()
        # time.sleep(1)
        ds = nc.Dataset("/scratch/gpfs/pk2354/DESC/GX/gx.nc")
        om = ds["Special/omega_v_time"][len(ds["Special/omega_v_time"]) - 1][:]
        # ky = ds['ky']
        # omega = np.zeros(len(om))
        gamma = np.zeros(len(om))
        for i in range(len(gamma)):
            #    #omega[i] = om[i][0][0]
            gamma[i] = om[i][0][1]
        print(max(gamma))

        t = str(time.time())
        nm = "gx" + t + ".nc"
        nmg = "gxinput_wrap" + t + ".out"
        # os.rename('/scratch/gpfs/pk2354/DESC/GX/gx.nc','/scratch/gpfs/pk2354/DESC/GX/' + nm)
        # os.rename('/scratch/gpfs/pk2354/DESC/GX/gxinput_wrap.out','/scratch/gpfs/pk2354/DESC/GX/' + nmg)
        os.rename(
            "/scratch/gpfs/pk2354/DESC/GX/gx.nc",
            "/scratch/gpfs/pk2354/DESC/GX/gx_old.nc",
        )
        os.rename(
            "/scratch/gpfs/pk2354/DESC/GX/gxinput_wrap.out",
            "/scratch/gpfs/pk2354/DESC/GX/gxinput_wrap_old.out",
        )

        # gamma = 1.0
        # print(gamma)
        return self._shift_scale(jnp.atleast_1d(max(gamma)))

    def compute_gx_jvp(self, values, tangents):
        # print("values are " + str(values))
        # print("tangents are " + str(tangents))
        R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi = values
        # primal_out = self.compute(R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi)
        primal_out = jnp.atleast_1d(0.0)

        n = len(values)
        argnum = np.arange(0, n, 1)
        # fd = FiniteDiffDerivative(self.compute,argnum)

        jvp = FiniteDiffDerivative.compute_jvp(
            self.compute, argnum, tangents, *values, rel_step=1e-4
        )

        return (primal_out, jvp)

    def compute_gx_batch(self, values, axis):
        print("AT BATCH!!!")
        print("VALUES IS " + str(values))

        numdiff = len(values[0])
        print("NUMDIFF IS " + str(numdiff))

        res = jnp.array([0.0])

        for i in range(numdiff):
            R_lmn = values[0][i]
            Z_lmn = values[1][i]
            L_lmn = values[2][i]
            i_l = values[3][i]
            p_l = values[4][i]
            Psi = values[5][i]

            res = jnp.vstack([res, self.compute(R_lmn, Z_lmn, L_lmn, i_l, p_l, Psi)])

        res = res[1:]

        return res, axis[0]

    def interp_to_new_grid(self, geo_array, zgrid, uniform_grid):
        # l = 2*nzgrid + 1
        geo_array_gx = np.zeros(len(geo_array))

        f = interp1d(zgrid, geo_array, kind="cubic")
        # print("The old grid is " + str(zgrid))
        # print("The new grid is " + str(uniform_grid))

        for i in range(len(geo_array_gx)):
            # print("zeta old is " + str(zgrid[i]))
            # print("zeta new is " + str(uniform_grid[i]))

            geo_array_gx[i] = f(np.round(uniform_grid[i], 5))

        return geo_array_gx

    def get_gx_arrays(
        self,
        zeta,
        bmag,
        grho,
        gradpar,
        gds2,
        gds21,
        gds22,
        gbdrift,
        gbdrift0,
        cvdrift,
        cvdrift0,
        sgn,
    ):
        dzeta = zeta[1] - zeta[0]
        dzeta_pi = np.pi / self.nzgrid
        index_of_middle = self.nzgrid

        gradpar_half_grid = np.zeros(2 * self.nzgrid)
        temp_grid = np.zeros(2 * self.nzgrid + 1)
        z_on_theta_grid = np.zeros(2 * self.nzgrid + 1)
        self.uniform_zgrid = np.zeros(2 * self.nzgrid + 1)

        gradpar_temp = np.copy(gradpar)

        for i in range(2 * self.nzgrid - 1):
            gradpar_half_grid[i] = 0.5 * (
                np.abs(gradpar[i]) + np.abs(gradpar_temp[i + 1])
            )
        gradpar_half_grid[2 * self.nzgrid - 1] = gradpar_half_grid[0]

        for i in range(2 * self.nzgrid):
            temp_grid[i + 1] = temp_grid[i] + dzeta * (1 / np.abs(gradpar_half_grid[i]))

        for i in range(2 * self.nzgrid + 1):
            z_on_theta_grid[i] = temp_grid[i] - temp_grid[index_of_middle]
        desired_gradpar = np.pi / np.abs(z_on_theta_grid[0])

        for i in range(2 * self.nzgrid + 1):
            z_on_theta_grid[i] = z_on_theta_grid[i] * desired_gradpar
            gradpar_temp[i] = desired_gradpar

        for i in range(2 * self.nzgrid + 1):
            self.uniform_zgrid[i] = z_on_theta_grid[0] + i * dzeta_pi

        final_theta_grid = self.uniform_zgrid

        self.bmag_gx = self.interp_to_new_grid(
            bmag, z_on_theta_grid, self.uniform_zgrid
        )
        self.grho_gx = self.interp_to_new_grid(
            grho, z_on_theta_grid, self.uniform_zgrid
        )
        self.gds2_gx = self.interp_to_new_grid(
            gds2, z_on_theta_grid, self.uniform_zgrid
        )
        self.gds21_gx = self.interp_to_new_grid(
            gds21, z_on_theta_grid, self.uniform_zgrid
        )
        self.gds22_gx = self.interp_to_new_grid(
            gds22, z_on_theta_grid, self.uniform_zgrid
        )
        self.gbdrift_gx = self.interp_to_new_grid(
            gbdrift, z_on_theta_grid, self.uniform_zgrid
        )
        self.gbdrift0_gx = self.interp_to_new_grid(
            gbdrift0, z_on_theta_grid, self.uniform_zgrid
        )
        self.cvdrift_gx = self.interp_to_new_grid(
            cvdrift, z_on_theta_grid, self.uniform_zgrid
        )
        self.cvdrift0_gx = self.interp_to_new_grid(
            cvdrift0, z_on_theta_grid, self.uniform_zgrid
        )
        self.gradpar_gx = gradpar_temp

        if sgn:
            self.gds21_gx = -self.gds21_gx
            self.gbdrift_gx = -self.gbdrift_gx
            self.gbdrift0_gx = -self.gbdrift0_gx
            self.cvdrift_gx = -self.cvdrift_gx
            self.cvdrift0_gx = -self.cvdrift0_gx

    def write_geo(self):
        nperiod = 1
        # rmaj = self.eq.compute('R0')['R0']
        kxfac = 1.0
        # print("At write geo: " + str(self.gbdrift_gx[0]) + str(self.gds2_gx[0]) + str(self.bmag_gx[0]))
        # open('gxinput_wrap.out', 'w').close()
        f = open("/scratch/gpfs/pk2354/DESC/GX/gxinput_wrap.out", "w")
        f.write("ntgrid nperiod ntheta drhodpsi rmaj shat kxfac q scale")
        f.write(
            "\n"
            + str(self.nzgrid)
            + " "
            + str(nperiod)
            + " "
            + str(2 * self.nzgrid)
            + " "
            + str(1.0)
            + " "
            + str(1 / self.Lref)
            + " "
            + str(self.shat)
            + " "
            + str(kxfac)
            + " "
            + str(1 / self.iota[0])
            + " "
            + str(2 * self.npol - 1)
        )

        f.write("\ngbdrift gradpar grho tgrid")
        for i in range(len(self.uniform_zgrid)):
            f.write(
                "\n"
                + str(self.gbdrift_gx[i])
                + " "
                + str(self.gradpar_gx[i])
                + " "
                + str(self.grho_gx[i])
                + " "
                + str(self.uniform_zgrid[i])
            )

        f.write("\ncvdrift gds2 bmag tgrid")
        for i in range(len(self.uniform_zgrid)):
            f.write(
                "\n"
                + str(self.cvdrift_gx[i])
                + " "
                + str(self.gds2_gx[i])
                + " "
                + str(self.bmag_gx[i])
                + " "
                + str(self.uniform_zgrid[i])
            )

        f.write("\ngds21 gds22 tgrid")
        for i in range(len(self.uniform_zgrid)):
            f.write(
                "\n"
                + str(self.gds21_gx[i])
                + " "
                + str(self.gds22_gx[i])
                + " "
                + str(self.uniform_zgrid[i])
            )

        f.write("\ncvdrift0 gbdrift0 tgrid")
        for i in range(len(self.uniform_zgrid)):
            f.write(
                "\n"
                + str(-self.cvdrift0_gx[i])
                + " "
                + str(-self.gbdrift0_gx[i])
                + " "
                + str(self.uniform_zgrid[i])
            )

        f.close()

    # def write_input(self)

    def run_gx(self):
        fs = open("stdout.out", "w")
        path = "/home/pk2354/src/gx/"
        # cmd = ['srun', '-N', '1', '-t', '00:10:00', '--ntasks=1', '--gpus-per-task=1', path+'./gx','/scratch/gpfs/pk2354/DESC/GX/gx.in']
        cmd = [path + "./gx", "/scratch/gpfs/pk2354/DESC/GX/gx.in"]
        # process = []
        # print(cmd)
        p = subprocess.run(cmd, stdout=fs)
        # p.wait()