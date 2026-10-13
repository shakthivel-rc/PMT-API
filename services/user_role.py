from models.roles import Role
from models.user_roles import UserRole
from fastapi import HTTPException
   
def add_roles_to_user(user, new_user, db):
    role = db.query(Role).filter(Role.id == user.role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    
    user_role = UserRole(role_id=role.id, user_id=new_user["id"])
    db.add(user_role)
   
    db.flush()
    
    