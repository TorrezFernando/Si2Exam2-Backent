"""Authorization rules shared by company administration and commerce."""
from fastapi import HTTPException
from api.deps import get_user_permissions


def is_global_admin(user):
    return bool(user.role and user.role.name == 'superadmin' and user.tenant_id is None)


def company_id(user):
    if user.tenant_id is None:
        raise HTTPException(403, 'Esta operación requiere un usuario de empresa.')
    if user.role and user.role.name == 'superadmin':
        raise HTTPException(403, 'Un rol global no puede pertenecer a una empresa.')
    return user.tenant_id


def check_company_admin(db, user, tenant_id):
    if is_global_admin(user):
        return
    if company_id(user) != tenant_id or 'branches:write' not in get_user_permissions(db, user):
        raise HTTPException(403, 'Se requiere administración de la propia empresa.')


def check_user_target(actor, target):
    if is_global_admin(actor):
        return
    if target.tenant_id != company_id(actor):
        raise HTTPException(404, 'Usuario no encontrado.')
    if target.role and target.role.name == 'superadmin':
        raise HTTPException(403, 'Solo el superadmin puede modificar usuarios globales.')


def check_assign_role(db, actor, role):
    if not role:
        raise HTTPException(404, 'Rol no encontrado.')
    if is_global_admin(actor):
        return
    company_id(actor)
    if role.name == 'superadmin' or not {p.name for p in role.permissions}.issubset(get_user_permissions(db, actor)):
        raise HTTPException(403, 'No puedes asignar un rol con privilegios superiores a los tuyos.')
