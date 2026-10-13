from pydantic import BaseModel

class PermissionCreate(BaseModel):
    permission_name: str
