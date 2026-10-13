from models.permissions import Permission
from models.role_permissions import RolePermission
from sqlalchemy.orm import Session

def fetch_permission(db: Session,skip:int,limit:int,order_by:str, search: str):
    if limit == 0:
        permissions = db.query(Permission).filter(Permission.name.ilike(f"%{search}%")).order_by(order_by).offset(skip).all()
        total_count = len(permissions)
        return permissions,total_count
    else:
        permissions = db.query(Permission).filter(Permission.name.ilike(f"%{search}%")).order_by(order_by).offset(skip).limit(limit).all()
        total_count = len(permissions)
        return permissions,total_count

def get_permission(permission_id, db):
    return db.query(Permission).filter(Permission.id == permission_id).first()

def get_role_permissions(permission_id, db):
    return db.query(RolePermission).filter(RolePermission.permission_id == permission_id).first()