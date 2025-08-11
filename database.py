from sqlalchemy import create_engine, Column, String, Integer, JSON, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.sql import func
from datetime import datetime
import time 
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
import logging
import uuid 
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    google_token = Column(JSON)
    created_at = Column(DateTime, default=func.now())
    processed_emails = relationship("ProcessedEmail", backref="user")
    calendar_events = relationship("CalendarEvent", backref="user")

class ProcessedEmail(Base):
    __tablename__ = 'processed_emails'
    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    thread_id = Column(String)
    subject = Column(String)
    from_email = Column(String)
    processed_at = Column(DateTime, default=func.now())
    category = Column(String)
    actions = Column(JSON)
    ai_response = Column(String)
    snippet = Column(String)
    sent_at = Column(DateTime, default=lambda: datetime.utcnow())
    calendar_event = relationship("CalendarEvent", backref="email", uselist=False) 

class CalendarEvent(Base):
    __tablename__ = 'calendar_events'
    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    email_id = Column(String, ForeignKey('processed_emails.id'))
    title = Column(String)
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    attendees = Column(JSON)
    created_at = Column(DateTime, default=func.now())
    timezone = Column(String)  
    description = Column(Text)  
    location = Column(String)  
    google_event_id = Column(String, unique=True)


def init_db(db_path='email_assistant.db'):
    engine = create_engine(f'sqlite:///{db_path}')
    Base.metadata.create_all(bind=engine)
    return engine

def create_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()

class DatabaseService:
    def __init__(self, db_path='email_assistant.db'):
        self.engine = create_engine(f'sqlite:///{db_path}')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        if not logging.root.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        if not hasattr(self.__class__, '_logger_configured'):
            self.logger = logging.getLogger(__name__)
            self.logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.__class__._logger_configured = True 
    def get_calendar_event_by_google_id(self, google_event_id: str):
        with self.Session() as session:
            return session.query(CalendarEvent).filter(CalendarEvent.google_event_id == google_event_id).first()

    def update_calendar_event(self, event_id: str, new_start_time, new_end_time, new_description=None):
        with self.Session() as session:
            event = session.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
            if event:
                event.start_time = new_start_time
                event.end_time = new_end_time
                if new_description is not None:
                    event.description = new_description
                session.commit()
                return True
            return False
    def get_user(self, email: str):
        with self.Session() as session:
            return session.query(User).filter(User.email == email).first()
    
    def create_user(self, email: str, token_data: dict):
        with self.Session() as session:
            try:
                user = User(email=email, google_token=token_data)
                session.add(user)
                session.commit()
                return user
            except IntegrityError:
                session.rollback()
                return None
    
    def update_user_token(self, email: str, token_data: dict):
        with self.Session() as session:
            user = session.query(User).filter(User.email == email).first()
            if user:
                user.google_token = token_data
                session.commit()
                return True
            return False
    
    def log_processed_email(self, email_data: dict):
        try:
            with self.Session() as session:
                email = ProcessedEmail(
                    id=email_data['id'],
                    user_id=email_data['user_id'],
                    thread_id=email_data.get('thread_id'),
                    subject=email_data['subject'],
                    from_email=email_data['from'],
                    category=email_data['category'],
                    actions=email_data['actions'],
                    ai_response=email_data.get('ai_response', ''),
                    snippet=email_data.get('snippet', ''),        
                    sent_at=email_data.get('sent_at')                
                )
                session.add(email)
                session.commit()
                self.logger.info(f"Logged email ID: {email_data['id']}")
                
        except IntegrityError as e:
            self.logger.warning(f"Duplicate email detected: {email_data['id']}")
            session.rollback()
            
        except SQLAlchemyError as e:
            self.logger.error(f"Database error logging email {email_data['id']}: {str(e)}")
            session.rollback()
            raise  
            
        except Exception as e:
            self.logger.critical(f"Unexpected error: {str(e)}", exc_info=True)
            session.rollback()
            raise
    
    def log_calendar_event(self, event_data: dict):
        with self.Session() as session:
            try:
                event = CalendarEvent(
                    user_id=event_data['user_id'],
                    email_id=event_data.get('email_id'),
                    title=event_data['title'],
                    start_time=event_data['start_time'],
                    end_time=event_data['end_time'],
                    attendees=event_data.get('attendees', []),
                    google_event_id=event_data.get('google_event_id'),
                    description=event_data.get('description', '')
                )
                session.add(event)
                session.commit()
                self.logger.info(f"✅ Calendar event saved: {event.google_event_id}")
            except Exception as e:
                session.rollback()
                self.logger.error(f"❌ Failed to save calendar event: {e}")
                raise
        
    def email_already_processed(self, email_id: str):
        with self.Session() as session:
            return session.query(ProcessedEmail).filter(
                ProcessedEmail.id == email_id
            ).first() is not None
        
    def get_calendar_event_by_thread(self, thread_id: str):
        with self.Session() as session:
            original_email = session.query(ProcessedEmail).filter(
                ProcessedEmail.thread_id == thread_id,
                ProcessedEmail.category == 'meeting'
            ).order_by(ProcessedEmail.sent_at.asc()).first()
            
            if original_email:
                return session.query(CalendarEvent).filter(
                    CalendarEvent.email_id == original_email.id
                ).first()
            return None
    def update_calendar_event_time(self, event_id: str, new_start_time: datetime, new_end_time: datetime) -> bool:
        with self.Session() as session:
            try:
                event = session.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
                if event:
                    event.start_time = new_start_time
                    event.end_time = new_end_time
                    session.commit()
                    self.logger.info(f"Updated calendar event: {event_id}")
                    return True
                return False
            except SQLAlchemyError as e:
                session.rollback()
                self.logger.error(f"Error updating calendar event {event_id}: {str(e)}")
                return False
            except Exception as e:
                session.rollback()
                self.logger.critical(f"Unexpected error updating event: {str(e)}", exc_info=True)
                return False
    def log_sent_email(self, email_data: dict):
        with self.Session() as session:
            try:
                sent_email = SentEmail(
                    user_id=email_data['user_id'],
                    thread_id=email_data.get('thread_id'),
                    message_id=email_data.get('message_id'),
                    recipient=email_data['recipient'],
                    subject=email_data['subject'],
                    body=email_data['body'],
                    context=email_data.get('context', '')
                )
                session.add(sent_email)
                session.commit()
                return True
            except Exception as e:
                print(f"Error logging sent email: {str(e)}")
                session.rollback()
                return False

class EmailThread(Base):
    __tablename__ = 'email_threads'
    id = Column(String, primary_key=True)  
    user_id = Column(Integer, ForeignKey('users.id'), index=True)
    subject = Column(String)
    last_processed = Column(DateTime)
class SentEmail(Base):
    __tablename__ = 'sent_emails'
    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Integer, ForeignKey('users.id'))
    thread_id = Column(String(100))
    message_id = Column(String(100))
    recipient = Column(String(255))
    subject = Column(String(255))
    body = Column(Text)
    sent_at = Column(DateTime, default=lambda: datetime.datetime.utcnow()) 
    context = Column(Text)
    
    def __repr__(self):
        return f"<SentEmail(to={self.recipient}, subject={self.subject[:20]}...)>"
if __name__ == "__main__":
    print("Initializing database...")
    engine = init_db()
    print("Database created successfully!")
    print(f"Database file: email_assistant.db")