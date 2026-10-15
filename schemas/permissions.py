from pydantic import BaseModel

class PermissionCreate(BaseModel):
    permission_name: str
    machine_name: str | None = None
