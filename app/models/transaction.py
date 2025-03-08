# models/transaction.py
from sqlalchemy import Column, Integer, String, Float, Enum, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(Integer, ForeignKey("users.id"))
    seller_id = Column(Integer, ForeignKey("users.id"))
    listing_id = Column(Integer, ForeignKey("listings.id"))
    amount = Column(Float)  # Amount in Pi
    fee = Column(Float)  # Platform fee (8%)
    net_amount = Column(Float)  # What seller receives
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING)
    pi_payment_id = Column(String, nullable=True)  # Reference to Pi payment
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    # Relationships
    buyer = relationship("User", foreign_keys=[buyer_id])
    seller = relationship("User", foreign_keys=[seller_id])
    listing = relationship("Listing")


# schemas/transaction.py
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models.transaction import TransactionStatus


class TransactionCreate(BaseModel):
    listing_id: int


class TransactionInDB(BaseModel):
    id: int
    buyer_id: int
    seller_id: int
    listing_id: int
    amount: float
    fee: float
    net_amount: float
    status: TransactionStatus
    pi_payment_id: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        orm_mode = True


class TransactionPublic(BaseModel):
    id: int
    listing_id: int
    amount: float
    status: TransactionStatus
    created_at: datetime
    completed_at: Optional[datetime] = None

    class Config:
        orm_mode = True


# routers/transactions.py
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
import requests
from datetime import datetime

from app.dependencies import get_db
from app.models.transaction import Transaction, TransactionStatus
from app.models.listing import Listing
from app.models.user import User
from app.schemas.transaction import TransactionCreate, TransactionPublic, TransactionInDB
from app.routers.auth import get_current_user

router = APIRouter()

# Constants
PLATFORM_FEE_PERCENT = 8.0  # 8%
PI_API_URL = "https://api.minepi.com/v2/payments"  # Example URL, replace with actual Pi API
PI_API_KEY = "YOUR_PI_API_KEY"  # Store in environment variables


# Helper function for Pi payment processing
async def process_pi_payment(transaction_id: int, db: Session):
    # Get transaction
    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not transaction or transaction.status != TransactionStatus.PENDING:
        return

    # In a real implementation, you would implement the Pi SDK
    # This is a placeholder for the actual Pi payment processing
    try:
        # Simulate payment processing
        # In a real implementation, you'd use the Pi SDK to create and verify a payment
        transaction.status = TransactionStatus.COMPLETED
        transaction.completed_at = datetime.utcnow()
        transaction.pi_payment_id = f"pi_payment_{transaction.id}"

        # Update seller stats
        seller = db.query(User).filter(User.id == transaction.seller_id).first()
        if seller:
            seller.total_sales += 1
            seller.total_revenue += transaction.net_amount

            # Update seller rating (simplified version)
            # In production, would use a more sophisticated approach based on reviews
            if seller.total_sales > 0:
                seller.seller_rating = min(5.0, seller.seller_rating + 0.1)

        db.commit()
    except Exception as e:
        # Log error
        transaction.status = TransactionStatus.FAILED
        transaction.notes = str(e)
        db.commit()


# Transaction endpoints
@router.post("/", response_model=TransactionPublic)
async def create_transaction(
        transaction_data: TransactionCreate,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    # Get listing
    listing = db.query(Listing).filter(Listing.id == transaction_data.listing_id, Listing.is_active == True).first()

    if not listing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found"
        )

    # Prevent buying own listing
    if listing.seller_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot purchase your own listing"
        )

    # Calculate fees
    amount = listing.price
    fee = amount * (PLATFORM_FEE_PERCENT / 100)
    net_amount = amount - fee

    # Create transaction
    transaction = Transaction(
        buyer_id=current_user.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        amount=amount,
        fee=fee,
        net_amount=net_amount,
        status=TransactionStatus.PENDING
    )

    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    # Process payment in background
    background_tasks.add_task(process_pi_payment, transaction.id, db)

    return transaction


@router.get("/", response_model=List[TransactionPublic])
async def get_user_transactions(
        status: Optional[TransactionStatus] = None,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    # Get transactions where user is either buyer or seller
    query = db.query(Transaction).filter(
        (Transaction.buyer_id == current_user.id) | (Transaction.seller_id == current_user.id)
    )

    if status:
        query = query.filter(Transaction.status == status)

    transactions = query.order_by(Transaction.created_at.desc()).all()
    return transactions


@router.get("/{transaction_id}", response_model=TransactionInDB)
async def get_transaction(
        transaction_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    # Get transaction
    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()

    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )

    # Verify user is buyer or seller
    if transaction.buyer_id != current_user.id and transaction.seller_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this transaction"
        )

    return transaction


# Endpoint to handle Pi payment callback
@router.post("/pi-callback")
async def pi_payment_callback(
        payment_data: dict,
        db: Session = Depends(get_db)
):
    # This would be a webhook endpoint for Pi payments
    # In a real implementation, you would verify the payment with Pi's SDK

    # Example implementation:
    payment_id = payment_data.get("payment_id")
    status = payment_data.get("status")
    transaction_id = payment_data.get("transaction_id")

    if not payment_id or not status or not transaction_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payment data"
        )

    # Get transaction
    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()

    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )

    # Update transaction status
    if status == "completed":
        transaction.status = TransactionStatus.COMPLETED
        transaction.completed_at = datetime.utcnow()
        transaction.pi_payment_id = payment_id

        # Update seller stats
        seller = db.query(User).filter(User.id == transaction.seller_id).first()
        if seller:
            seller.total_sales += 1
            seller.total_revenue += transaction.net_amount

            # Update seller rating (simplified)
            if seller.total_sales > 0:
                seller.seller_rating = min(5.0, seller.seller_rating + 0.1)
    elif status == "failed":
        transaction.status = TransactionStatus.FAILED
        transaction.notes = payment_data.get("error", "Payment failed")

    db.commit()

    return {"status": "success"}