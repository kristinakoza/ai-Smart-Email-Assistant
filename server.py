import os
import base64
import json
import uuid
import time
from flask import Flask, jsonify, request, session, redirect, url_for
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from email.message import EmailMessage
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config import REDIRECT_URI, BASE_DIR, TOKEN_PATH, CREDENTIALS_PATH, WEB_CREDENTIALS_PATH
from datetime import timedelta, datetime
import pytz
from core import EmailClassifier, GmailClient, CalendarClient, EmailProcessor, DUBAI_TIMEZONE
from dateutil import parser
from dotenv import load_dotenv

# Load .env from project root
load_dotenv()


DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai')
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
API_URL = "https://api.deepseek.com/v1/chat/completions"

# Gmail API configuration
CLIENT_SECRETS_FILE = WEB_CREDENTIALS_PATH
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar.events'
]
# app configuration
app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")
app.config.update(
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=5)
)

# Initialize core services
email_classifier = EmailClassifier()

def get_email_processor():
    if 'credentials' not in session:
        return None
        
    creds_dict = session['credentials']
    creds = Credentials(
        token=creds_dict['token'],
        refresh_token=creds_dict['refresh_token'],
        token_uri=creds_dict['token_uri'],
        client_id=creds_dict['client_id'],
        client_secret=creds_dict['client_secret'],
        scopes=creds_dict['scopes']
    )

    gmail_client = GmailClient(credentials=creds)
    calendar_client = CalendarClient(credentials=creds)
    
    # Revert to original implementation
    return EmailProcessor(gmail_client, calendar_client)
# Mock data storage
processed_items = []
events = []
failed_items = []
drafts = []

@app.route('/api/counts', methods=['GET'])
def get_counts():
    return jsonify({
        "processed": len(processed_items),
        "failed": len(failed_items),
        "drafts": len(drafts),
        "events": len(events)
    })

@app.route('/api/processed', methods=['GET'])
def get_processed():
    page = int(request.args.get('page', 1))
    page_size = 10
    start = (page - 1) * page_size
    end = start + page_size
    items = processed_items[start:end]
    return jsonify({
        "items": items,
        "total_pages": (len(processed_items) + page_size - 1) // page_size
    })

@app.route('/api/events', methods=['GET'])
def get_events():
    """Return calendar events (mock implementation)"""
    return jsonify({"items": events})

@app.route('/api/failed', methods=['GET'])
def get_failed():
    """Return failed items (mock implementation)"""
    return jsonify({"items": failed_items})

@app.route('/api/search', methods=['GET'])
def search():
    """Search endpoint (mock implementation)"""
    query = request.args.get('q', '')
    return jsonify({
        "items": [{
            "title": f"Search result for {query}",
            "preview": "Sample search result",
            "date": "2023-08-16"
        }]
    })

@app.route('/api/process/<id>', methods=['POST'])
def process_email(id):
    processor = get_email_processor()
    if not processor:
        return jsonify({"error": "Not authenticated"}), 401
        
    try:
        email = processor.gmail.get_email_content(id)
        if not email:
            return jsonify({"error": "Email not found"}), 404
            
        classification = processor.classifier.classify_email(email.get('body', ''))
        
        actions = {"processed": True}
        if processor.is_meeting_request(email.get('body', '')):
            meeting_actions = processor.handle_meeting_email(email)
            actions.update(meeting_actions)
        
        # Store processed email
        processed_item = {
            "id": id,
            "subject": email.get('subject', 'No Subject'),
            "from": email.get('from', ''),
            "classification": classification,
            "actions": actions,
            "date": datetime.now().isoformat()
        }
        processed_items.append(processed_item)
            
        return jsonify({
            "status": "processed",
            "classification": classification,
            "actions": actions
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/api/processed/<id>', methods=['GET'])
def get_processed_email(id):
    """Get processed email metadata by ID"""
    # Find processed item by ID
    item = next((item for item in processed_items if item['id'] == id), None)
    
    if item:
        return jsonify(item)
    else:
        return jsonify({"error": "Processed email not found"}), 404    

def safe_credentials_to_dict(credentials):
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def get_gmail_service():
    if 'credentials' not in session:
        return None
    
    creds_dict = session['credentials']
    creds = Credentials(
        token=creds_dict['token'],
        refresh_token=creds_dict['refresh_token'],
        token_uri=creds_dict['token_uri'],
        client_id=creds_dict['client_id'],
        client_secret=creds_dict['client_secret'],
        scopes=creds_dict['scopes']
    )
    
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            session['credentials'] = safe_credentials_to_dict(creds)
            session.modified = True
        except Exception as e:
            print(f"Error refreshing token: {e}")
            return None
    
    return build('gmail', 'v1', credentials=creds)

@app.route('/')
def index():
    # Check if we have valid credentials
    if 'credentials' in session and session['credentials'].get('token'):
        # Try to create a service to verify credentials are still valid
        service = get_gmail_service()
        if service:
            return app.send_static_file('index.html')
        else:
            # Credentials are invalid, clear session and re-authorize
            session.clear()
    
    return redirect(url_for('authorize'))

@app.route('/authorize')
def authorize():
    # Clear any existing session data to prevent loops and scope mismatches
    session.clear()
    
    print(f"Using redirect URI: {REDIRECT_URI}")
    print(f"Using client secrets file: {CLIENT_SECRETS_FILE}")
    print(f"Requesting scopes: {SCOPES}")
    
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"  # Force consent to clear any cached scope mismatches
    )

    print(f"Generated authorization URL: {authorization_url}")
    session['state'] = state
    session.modified = True
    return redirect(authorization_url)

# Update the oauth2callback route
@app.route('/oauth2callback')
def oauth2callback():
    print("OAuth callback received")
    # Get state from session and request
    stored_state = session.get('state')
    request_state = request.args.get('state')
    
    print(f"Stored state: {stored_state}")
    print(f"Request state: {request_state}")
    
    # Verify state matches
    if not stored_state or stored_state != request_state:
        return jsonify({'status': 'error', 'message': 'Invalid OAuth state'}), 400
    
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        state=stored_state,
        redirect_uri=REDIRECT_URI  # Use the correct redirect URI
    )
    
    try:
        # Fetch tokens
        print("Fetching tokens...")
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        
        # Store credentials in session
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        session.modified = True
        print("Credentials stored in session")
        
        # Redirect to main app
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Error in oauth2callback: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/emails')
def get_emails():
    processor = get_email_processor()
    if not processor:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        page = int(request.args.get('page', 1))
        page_size = 10
        
        # Get recent emails (last 24 hours)
        cutoff = datetime.now(pytz.utc) - timedelta(hours=24)
        emails = processor.gmail.get_recent_emails(
            max_results=page_size * 2, 
            after_date=cutoff.isoformat()
        )
        
        formatted_emails = []
        for email in emails:
            # Extract date from email if available
            received_at = ""
            try:
                if 'date' in email:
                    received_at = email['date']
            except KeyError:
                pass
                
            formatted_emails.append({
                'id': email['id'],
                'from': email['from'],
                'from_name': email['from'].split('<')[0].strip(),
                'subject': email['subject'],
                'snippet': email['snippet'],
                'received_at': received_at,
                'read': False  # Placeholder
            })
        
        # Pagination
        start_idx = (page - 1) * page_size
        paginated_emails = formatted_emails[start_idx:start_idx + page_size]
        
        return jsonify({
            "items": paginated_emails,
            "total_pages": (len(emails) + page_size - 1) // page_size
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/emails/<id>')
def get_email(id):
    processor = get_email_processor()
    if not processor:
        return jsonify({"error": "Not authenticated"}), 401
    
    try:
        email = processor.gmail.get_email_content(id)
        if not email:
            return jsonify({"error": "Email not found"}), 404
        
        processed_data = next((item for item in processed_items if item['id'] == id), None)
        if processed_data:
            email['processed'] = processed_data
            
        return jsonify({
            'id': id,
            'from': email['from'],
            'from_name': email['from'].split('<')[0].strip(),
            'subject': email['subject'],
            'received_at': email['date'],
            'body': email['body'],
            'processed': processed_data  
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/api/send', methods=['POST'])
def send_email():
    processor = get_email_processor()
    if not processor:
        return jsonify({"error": "Not authenticated"}), 401
        
    try:
        data = request.json
        to = data.get('to', '')
        subject = data.get('subject', '')
        body = data.get('body', '')
        thread_id = data.get('thread_id', None)
        
        result = processor.gmail.send_email(to, subject, body, thread_id)
        
        if result.get('status') == 'success':
            return jsonify({
                "status": "sent",
                "id": result.get('message_id')
            })
        else:
            return jsonify({
                "error": result.get('error_message', 'Failed to send email')
            }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/drafts', methods=['POST'])
def save_draft():
    data = request.json
    draft = {
        "id": str(uuid.uuid4()),
        "to": data.get('to', ''),
        "subject": data.get('subject', ''),
        "body": data.get('body', ''),
        "saved_at": time.strftime("%Y-%m-%d %H:%M")
    }
    drafts.append(draft)
    return jsonify({"status": "draft_saved", "id": draft["id"]})

@app.route('/api/generate', methods=['POST'])
def generate_draft():
    try:
        data = request.json
        prompt = data.get('prompt', '')
        
        generated = email_classifier._call_ai_api(f"Write a professional email about: {prompt}")
        
        # Get an EmailProcessor instance to clean the email
        processor = get_email_processor()
        if not processor:
            return jsonify({"error": "Not authenticated"}), 401
            
        cleaned = processor.clean_generated_email(generated)
        
        return jsonify({"text": cleaned})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/api/schedule', methods=['POST'])
def schedule_event():
    processor = get_email_processor()
    if not processor:
        return jsonify({"error": "Not authenticated"}), 401
        
    try:
        data = request.json
        title = data.get('title', '')
        start_str = data.get('start', '')
        duration = data.get('duration', 60)
        attendees = data.get('attendees', [])
        
        # Parse and validate times
        start_time = parser.parse(start_str)
        end_time = start_time + timedelta(minutes=duration)
        
        # Create event
        event_result = processor.calendar.create_event(
            summary=title,
            start_time=start_time,
            end_time=end_time,
            attendees=attendees
        )
        
        if event_result:
            return jsonify({
                "status": "scheduled",
                "id": event_result['id'],
                "link": event_result.get('link', '')
            })
        else:
            return jsonify({"error": "Failed to create event"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('authorize'))

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

if __name__ == '__main__':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    app.run(host='localhost', port=5000, debug=True)