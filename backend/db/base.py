"""
Single declarative Base for all SmartTrade SQLite/SQLAlchemy ORM models.

Kept strictly free of I/O, dotenv loading, and Engine construction so importing
any ORM model never touches the filesystem or production database settings.
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
