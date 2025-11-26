from pydantic import BaseModel
from typing import Optional

class UserBase(BaseModel):
    user_type: str
    last_name: str
    first_name: str
    middle_name: Optional[str] = None
    group_name: Optional[str] = None

class UserCreate(UserBase):
    pass

class User(UserBase):
    id: int
    login: str
    password: str
    
    class Config:
        from_attributes = True

class UserUpdate(BaseModel):
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    group_name: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None