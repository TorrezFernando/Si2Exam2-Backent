"""Resolve public storefront slugs without accepting an authorization tenant ID."""
from fastapi import HTTPException
from db.models.tenant import Tenant


def selected_store(db, user, slug=None, request=None):
    if user:
        if not user.is_active:
            raise HTTPException(403, 'Usuario inactivo.')
        if user.tenant_id is not None:
            tenant = db.query(Tenant).filter_by(id=user.tenant_id, is_active=True).first()
            if not tenant: raise HTTPException(403, 'Empresa no disponible.')
            return tenant
        if not user.role or user.role.name != 'superadmin':
            raise HTTPException(403, 'Usuario sin empresa asignada.')
    if not slug:
        context_id = request.headers.get('x-tenant-id') if request and user and user.role and user.role.name == 'superadmin' else None
        if context_id:
            try: tenant_id = int(context_id)
            except ValueError: raise HTTPException(400, 'Contexto de empresa inválido.')
            tenant = db.query(Tenant).filter_by(id=tenant_id, is_active=True).first()
            if not tenant: raise HTTPException(404, 'Empresa no disponible.')
            return tenant
        return None
    tenant = db.query(Tenant).filter_by(subdomain=slug, is_active=True).first()
    if not tenant: raise HTTPException(404, 'Tienda no encontrada.')
    return tenant


def storefront_metadata(db, user, slug=None, request=None):
    selected = selected_store(db, user, slug, request)
    stores = [selected] if user and user.tenant_id is not None else db.query(Tenant).filter_by(is_active=True).order_by(Tenant.name).all()
    def public(t): return {'name': t.name, 'slug': t.subdomain}
    return {'selected': public(selected) if selected else None, 'stores': [public(t) for t in stores]}
