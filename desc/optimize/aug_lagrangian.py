#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon May 30 15:25:26 2022

@author: pk123
"""
import numpy as np
from termcolor import colored
from desc.backend import jnp
from .derivative import CholeskyHessian
from .utils import (
    check_termination,
    evaluate_quadratic_form,
    print_header_nonlinear,
    print_iteration_nonlinear,
    status_messages,
)
from .tr_subproblems import (
    solve_trust_region_dogleg,
    solve_trust_region_2d_subspace,
    update_tr_radius,
)
from scipy.optimize import OptimizeResult
from scipy.optimize import minimize

from desc.optimize.fmin_scalar import fmintr

#from desc.objective_funs import AugLagrangian
from desc.objectives.auglagrangian_objectives import AugLagrangian
from desc.derivatives import Derivative

# def gradL(x,fun,lmbda,mu,c,gradc):
#     return

# def L(x,fun,lmbda,mu,c,gradc):    
#     return fun(x) - np.dot(lmbda,c(x)) + mu/2*np.dot(c(x),c(x))

# def proj(x,l,u):
#     if all(x - l > np.zeros(len(x))) and all(u - x > np.zeros(len(x))):
#         return x
#     else:
#         if not (all(x - l > np.zeros(len(x)))):
#             return l
#         else:
#             return u
        
# def conv_test(x,gL,l,u):
#     if not (l and u):
#         return np.linalg.norm(gL)
#     else:
#         return np.linalg.norm(x - proj(x - gL, l, u))
    

def conv_test(x,gL,l,u):
    return np.linalg.norm(gL)

# def bound_constr(x,lmbda,mu,gradL)

def fmin_lag(
    fun,
    x0,
    lmbda0,
    mu0,
    grad,
    constr,
    gradconstr,
    ineq,
    gradineq,
    l = None,
    u = None,
    hess="bfgs",
    args=(),
    method="dogleg",
    x_scale=1,
    ftol=1e-6,
    xtol=1e-6,
    gtol=1e-6,
    ctol=1e-6,
    verbose=1,
    maxiter=None,
    callback=None,
    options={},
):
    nfev = 0
    ngev = 0
    nhev = 0
    iteration = 0

    N = x0.size
    x = x0.copy()
    f = fun(x, *args)
    nfev += 1
    g = grad(x, *args)
    ngev += 1
    lmbda = lmbda0
    
    constr = np.concatenate((constr,ineq),axis=None)
    gradconstr = np.concatenate((gradconstr,gradineq),axis=None)
    
    L = AugLagrangian(fun, constr)
    gradL = Derivative(L.compute,argnum=0)
    hessL = Derivative(L.compute,argnum=0,mode="hess")

    mu = mu0
    gtolk = 1/(10*mu0)
    ctolk = 1/(mu0**(0.1))    
    xold = x
    
    
    while iteration < maxiter:
        print("Before minimize\n")
        xk = fmintr(L.compute,x,gradL,hess = hessL,args=(lmbda,mu),gtol=gtolk,maxiter = maxiter)
        #xk = minimize(L.compute,x,args=(lmbda,mu),method="trust-constr",jac=gradL)
        print("After minimize\n")
        x = xk['x']

        c = 0
        
        cv = L.compute_constraints(x)
        c = np.linalg.norm(cv)
        print("The constraints are " + str(cv))
        print("The gradient is " + str(gradL(x,lmbda,mu)))
        if np.linalg.norm(xold - x) < xtol:
            print("xtol satisfied\n")
            break        
        
        if c < ctolk:

            if c < ctol and conv_test(x,gradL(x,lmbda,mu),l,u) < gtol:
                break

            else:
                lmbda = lmbda - mu*cv
                ctolk = ctolk/(mu**(0.9))
                gtolk = gtolk/mu
        else:
            mu = 100 * mu
            ctolk = 1/(mu**(0.1))
            gtolk = gtolk/mu
        
        
        iteration = iteration + 1
        xold = x
        print(fun(x))
        print(x)
    return [fun(x),x,lmbda,c,gradL(x,lmbda,mu)]