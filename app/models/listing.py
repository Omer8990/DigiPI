# models/listing.py
from sqlalchemy import Column, Integer, String, Float, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    description = Column(Text, nullable=True)


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, index=True)
    description = Column(Text)
    price = Column(Float)  # Price in Pi
    seller_id = Column(Integer, ForeignKey("users.id"))
    category_id = Column(Integer, ForeignKey("categories.id"))
    file_path = Column(String)  # S3 path to file
    thumbnail_path = Column(String, nullable=True)  # S3 path to thumbnail
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    seller = relationship("User", back_populates="listings")
    category = relationship("Category")
    reviews = relationship("Review", back_populates="listing")


# Add this to User model
User.listings = relationship("Listing", back_populates="seller")

# schemas/listing.py
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class CategoryBase(BaseModel):
    name: str
    description: Optional[str] = None


class CategoryCreate(CategoryBase):
    pass


class CategoryInDB(CategoryBase):
    id: int

    class Config:
        orm_mode = True


class ListingBase(BaseModel):
    title: str
    description: str
    price: float = Field(..., gt=0)  # Price must be positive
    category_id: int


class ListingCreate(ListingBase):
    pass  # File handling will be done separately


class ListingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = Field(None, gt=0)
    category_id: Optional[int] = None
    is_active: Optional[bool] = None


class ListingInDB(ListingBase):
    id: int
    seller_id: int
    file_path: str
    thumbnail_path: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class ListingPublic(BaseModel):
    id: int
    title: str
    description: str
    price: float
    seller_id: int
    category: CategoryInDB
    thumbnail_path: Optional[str] = None
    created_at: datetime
    avg_rating: Optional[float] = None

    class Config:
        orm_mode = True


# routers/listings.py
from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import boto3
import uuid
from datetime import datetime

from app.dependencies import get_db
from app.models.listing import Listing, Category
from app.models.user import User
from app.schemas.listing import ListingCreate, ListingUpdate, ListingPublic, CategoryInDB
from app.routers.auth import get_current_user

router = APIRouter()

# S3 client setup - would move to service in production
s3_client = boto3.client(
    's3',
    aws_access_key_id='YOUR_ACCESS_KEY',
    aws_secret_access_key='YOUR_SECRET_KEY'
)
BUCKET_NAME = 'pi-marketplace-files'


# Category endpoints
@router.get("/categories", response_model=List[CategoryInDB])
async def get_categories(db: Session = Depends(get_db)):
    categories = db.query(Category).all()
    return categories


# Listing endpoints
@router.post("/", response_model=ListingPublic)
async def create_listing(
        title: str = Form(...),
        description: str = Form(...),
        price: float = Form(...),
        category_id: int = Form(...),
        file: UploadFile = File(...),
        thumbnail: Optional[UploadFile] = File(None),
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    # Check if category exists
    category = db.query(Category).filter(Category.id == category_id).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    # Upload main file to S3
    file_extension = file.filename.split('.')[-1]
    file_name = f"{uuid.uuid4()}.{file_extension}"
    file_path = f"files/{current_user.id}/{file_name}"

    s3_client.upload_fileobj(file.file, BUCKET_NAME, file_path)

    # Upload thumbnail if provided
    thumbnail_path = None
    if thumbnail:
        thumb_extension = thumbnail.filename.split('.')[-1]
        thumb_name = f"{uuid.uuid4()}.{thumb_extension}"
        thumbnail_path = f"thumbnails/{current_user.id}/{thumb_name}"
        s3_client.upload_fileobj(thumbnail.file, BUCKET_NAME, thumbnail_path)

    # Create listing in database
    db_listing = Listing(
        title=title,
        description=description,
        price=price,
        seller_id=current_user.id,
        category_id=category_id,
        file_path=file_path,
        thumbnail_path=thumbnail_path
    )

    db.add(db_listing)
    db.commit()
    db.refresh(db_listing)

    # Calculate average rating
    avg_rating = 0.0  # For new listings

    # Prepare response
    response = ListingPublic(
        id=db_listing.id,
        title=db_listing.title,
        description=db_listing.description,
        price=db_listing.price,
        seller_id=db_listing.seller_id,
        category=category,
        thumbnail_path=db_listing.thumbnail_path,
        created_at=db_listing.created_at,
        avg_rating=avg_rating
    )

    return response


@router.get("/", response_model=List[ListingPublic])
async def get_listings(
        category_id: Optional[int] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
        db: Session = Depends(get_db)
):
    query = db.query(Listing).filter(Listing.is_active == True)

    if category_id:
        query = query.filter(Listing.category_id == category_id)

    if search:
        search_term = f"%{search}%"
        query = query.filter(Listing.title.ilike(search_term) |
                             Listing.description.ilike(search_term))

    listings = query.offset(skip).limit(limit).all()

    # Transform to response model with category info
    result = []
    for listing in listings:
        # Get category
        category = db.query(Category).filter(Category.id == listing.category_id).first()

        # Calculate average rating
        # This would be more efficient with a separate query or denormalization in production
        avg_rating = 0.0  # Placeholder, would calculate from reviews

        item = ListingPublic(
            id=listing.id,
            title=listing.title,
            description=listing.description,
            price=listing.price,
            seller_id=listing.seller_id,
            category=category,
            thumbnail_path=listing.thumbnail_path,
            created_at=listing.created_at,
            avg_rating=avg_rating
        )
        result.append(item)

    return result


@router.get("/{listing_id}", response_model=ListingPublic)
async def get_listing(listing_id: int, db: Session = Depends(get_db)):
    listing = db.query(Listing).filter(Listing.id == listing_id, Listing.is_active == True).first()

    if not listing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found"
        )

    # Get category
    category = db.query(Category).filter(Category.id == listing.category_id).first()

    # Calculate average rating
    # This would be more efficient with a separate query or denormalization in production
    avg_rating = 0.0  # Placeholder, would calculate from reviews

    return ListingPublic(
        id=listing.id,
        title=listing.title,
        description=listing.description,
        price=listing.price,
        seller_id=listing.seller_id,
        category=category,
        thumbnail_path=listing.thumbnail_path,
        created_at=listing.created_at,
        avg_rating=avg_rating
    )