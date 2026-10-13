"""ORM 模型。全部表在此包内定义，导入本包即完成元数据注册。"""
from app.models.account import Account
from app.models.category import Category
from app.models.company import Company, Contact
from app.models.label import Label, MessageLabel
from app.models.message import Attachment, Message
from app.models.rule import Rule, Setting
from app.models.sync_state import SyncState
from app.models.summary import Summary

__all__ = [
    "Account",
    "Category",
    "Company",
    "Contact",
    "Label",
    "MessageLabel",
    "Attachment",
    "Message",
    "Rule",
    "Setting",
    "SyncState",
    "Summary",
]