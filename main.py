import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from dotenv import load_dotenv
import json
import re
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from typing import List, Dict
import base64
import pytz
import dateparser
from dateutil import parser, tz
from typing import Optional, Dict, List 
from database import DatabaseService
from urllib.parse import urlparse, parse_qs
from dateparser import parse
from dateparser.conf import settings
from email.utils import parsedate_to_datetime
DUBAI_TIMEZONE = pytz.timezone('Asia/Dubai')

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
API_URL = "https://api.deepseek.com/v1/chat/completions"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly", 
    "https://www.googleapis.com/auth/calendar.events"
]

class EmailClassifier:
    def classify_email(self, query: str) -> dict:
        query_lower = query.lower()
        category = "general"
        emotional_tone = "neutral"
        priority = "normal"
        suggested_response_time = "24h"
        
        if any(word in query_lower for word in ["thank", "appreciate", "grateful"]):
            category = "gratitude"
            emotional_tone = "positive"
        elif any(word in query_lower for word in ["meeting", "call", "schedule"]):
            category = "meeting"
        elif any(word in query_lower for word in ["birthday", "anniversary", "congrats"]):
            category = "celebration"
            emotional_tone = "positive"
        elif any(word in query_lower for word in ["urgent", "asap", "important"]):
            priority = "urgent"
            suggested_response_time = "1h"
        
        return {
            "category": category,
            "emotional_tone": emotional_tone,
            "priority": priority,
            "suggested_response_time": suggested_response_time
        }
    def summarize_meeting(self, email_body: str) -> str:
        prompt = f"""Summarize this meeting request into a concise 1-2 sentence description focusing on:
        - Purpose of meeting
        - Key topics
        - Any special instructions
        
        Email content:
        {email_body[:2000]}"""
        
        try:
            summary = self._call_ai_api(prompt)
            summary = re.sub(r'^Summary:\s*', '', summary)  
            summary = re.sub(r'\s+', ' ', summary).strip()  
            return summary[:500]  
        except Exception as e:
            print(f"⚠️ Summary generation failed: {str(e)}")
            return "Meeting scheduled via email"  
    def _call_ai_api(self, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        payload = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 1000
        }

        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"API Error: {str(e)}")
            return ""

class GmailClient:
    def __init__(self):
        self.service = self._authenticate()
    
    def _authenticate(self):
        creds = None
        db = DatabaseService()
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        
        service = build('gmail', 'v1', credentials=creds)
        
        profile = service.users().getProfile(userId='me').execute()
        user_email = profile['emailAddress']
        token_data = json.loads(creds.to_json())
        
        user = db.get_user(user_email)
        if user:
            db.update_user_token(user_email, token_data)
        else:
            db.create_user(user_email, token_data)

        return service
        
    def get_recent_emails(self, max_results=5, after_date=None) -> List[Dict[str, str]]:
        try:
            query = 'in:inbox'
            if after_date:
                formatted_date = after_date.split('T')[0].replace('-', '/')
                query += f' after:{formatted_date}'
                
            results = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            
            messages = results.get('messages', [])
            email_data = []
            
            for msg in messages:
                msg_detail = self.service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='metadata',
                    metadataHeaders=['From', 'To', 'Subject']
                ).execute()
                
                headers = msg_detail.get("payload", {}).get("headers", [])
                from_email = next((h['value'] for h in headers if h['name'] == 'From'), '')
                to_email = next((h['value'] for h in headers if h['name'] == 'To'), '')
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                
                email_data.append({
                    'id': msg['id'],
                    'from': from_email,
                    'to': to_email,
                    'subject': subject,
                    'snippet': msg_detail.get('snippet', '')
                })
            
            return email_data
            
        except Exception as error:
            print(f"Error fetching emails: {str(error)}")
            return []
    
    def send_email(self, to: str, subject: str, body: str, thread_id: str = None) -> str:
        message = self._create_message(to, subject, body, thread_id)
        try:
            result = self.service.users().messages().send(
                userId="me",
                body=message
            ).execute()
            return {
            'status': 'success',
            'message_id': result['id'],
            'thread_id': thread_id,
            'raw_response': f"Message Id: {result['id']}"
            }
        except Exception as e:
            error_msg = f"Failed to send email: {str(e)}"
            print(error_msg)
            return {
            'status': 'error',
            'error_message': error_msg
            }

    def _format_email(self, content: str) -> str:
        """Ensure proper email formatting"""
        if not content.strip().startswith(("Dear", "Hello", "Hi")):
            content = f"Dear Recipient,\n\n{content}"
        if not re.search(r"(Sincerely|Regards|Best),?$", content, re.IGNORECASE):
            content += "\n\nSincerely,\n[Your Name]"
        return content

    def _create_message(self, to: str, subject: str, message_text: str, thread_id: str = None) -> dict:
        email_text = f"To: {to}\nSubject: {subject}\n\n{message_text}"
        message = {
            'raw': base64.urlsafe_b64encode(email_text.encode()).decode(),
            'threadId': thread_id
        }
        return {k: v for k, v in message.items() if v is not None}
    
    def test_gmail_connection(self):
        try:
            profile = self.service.users().getProfile(userId='me').execute()
            print("Gmail API connection successful!")
            print(f"Connected as: {profile['emailAddress']}")
            return True
        except Exception as e:
            print("Gmail connection failed!")
            print(f"Error: {str(e)}")
            return False
            
    def get_email_content(self, message_id: str) -> Dict[str, str]:
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            
            headers = {}
            for h in message['payload'].get('headers', []):
                if h['name'].lower() in ['from', 'to', 'subject', 'date']:
                    headers[h['name'].lower()] = h['value']
            
            body = ""
            if 'parts' in message['payload']:
                for part in message['payload']['parts']:
                    if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                        body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                        break
            elif 'body' in message['payload'] and 'data' in message['payload']['body']:
                body = base64.urlsafe_b64decode(message['payload']['body']['data']).decode('utf-8')
            
            return {
                'id': message['id'],
                'threadId': message.get('threadId'),
                'subject': headers.get('subject', 'No Subject'),
                'from': headers.get('from'),
                'to': headers.get('to'),
                'date': headers.get('date'),
                'body': body
            }
        
        except Exception as e:
            print(f"Error reading email: {str(e)}")
            return {}
class CalendarClient:
    def __init__(self):
        self.service = self._authenticate()
        self.db = DatabaseService()
    
    def _authenticate(self):
        creds = None
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        
        return build('calendar', 'v3', credentials=creds)
    
    def test_connection(self): #Kind of debug code, testing the connection cuz it was disconnecting before 
        try:
            events_result = self.service.events().list(
                calendarId='primary',
                maxResults=1,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            print("Calendar API connected! Found", len(events), "event(s).")
            return True
        except Exception as e:
            print("Calendar API connection failed!")
            print("Error:", e)
            return False
    
    def create_event(self, summary, start_time, end_time, attendees=None, location=None, description=None):
        try:
            dubai_tz = pytz.timezone('Asia/Dubai')  # Dubai timezone
            start_time = start_time.astimezone(dubai_tz)
            end_time = end_time.astimezone(dubai_tz)
            
            event = {
                'summary': summary,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': 'Asia/Dubai',
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': 'Asia/Dubai',
                },
            }
            
            if attendees:
                event['attendees'] = [{'email': email} for email in attendees]
            
            if location:
                event['location'] = location
                
            if description:
                event['description'] = description
            
            created_event = self.service.events().insert(
                calendarId='primary',
                body=event
            ).execute()
            
            return {
                'id': created_event['id'], 
                'link': created_event.get('htmlLink', '')  
            }
            
        except Exception as e:
            print(f"Failed to create calendar event: {str(e)}")
            return None
        
    def update_event(self, event_id, new_start_time=None, new_end_time=None, 
                   summary=None, description=None, attendees=None, location=None):
        try:
            event = self.service.events().get(
                calendarId='primary',
                eventId=event_id
            ).execute()
            
            if new_start_time:
                new_start_time = new_start_time.astimezone(pytz.timezone('Asia/Dubai'))
                event['start'] = {
                    'dateTime': new_start_time.isoformat(),
                    'timeZone': 'Asia/Dubai',
                }
            if new_end_time:
                new_end_time = new_end_time.astimezone(pytz.timezone('Asia/Dubai'))
                event['end'] = {
                    'dateTime': new_end_time.isoformat(),
                    'timeZone': 'Asia/Dubai',
                }
            
            if summary:
                event['summary'] = summary
            if description:
                event['description'] = description
            if attendees:
                event['attendees'] = [{'email': email} for email in attendees]
            if location:
                event['location'] = location
                
            updated_event = self.service.events().update(
                calendarId='primary',
                eventId=event_id,
                body=event
            ).execute()
            
            return {
                'id': updated_event['id'],
                'link': updated_event.get('htmlLink', '')
            }
        except Exception as e:
            print(f"Failed to update calendar event: {str(e)}")
            return None
           
    def list_events(self, max_results=25):
        try:
            now = datetime.utcnow().isoformat() + 'Z'
            events_result = self.service.events().list(
                calendarId='primary',
                timeMin=now,
                maxResults=max_results,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            return events_result.get('items', [])
        except Exception as e:
            print(f" Failed to list events: {str(e)}")
            return []
    
    def delete_event(self, event_id):
        try:
            self.service.events().delete(
                calendarId='primary',
                eventId=event_id
            ).execute()
            print(f"✅ Event deleted successfully!")
            return True
        except Exception as e:
            print(f"❌ Failed to delete event: {str(e)}")
            return False

    def check_availability(self, start_time: datetime, end_time: datetime) -> bool:
        try:
            start_rfc = start_time.isoformat()
            end_rfc = end_time.isoformat()
            
            events_result = self.service.events().list(
                calendarId='primary',
                timeMin=start_rfc,
                timeMax=end_rfc,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            return len(events) == 0
            
        except Exception as e:
            print(f"❌ Error checking availability: {str(e)}")
            return False
    
    def get_calendar_timezone(self):
        try:
            calendar = self.service.calendars().get(calendarId='primary').execute()
            return calendar.get('timeZone', 'UTC')
        except Exception as e:
            print(f"❌ Failed to get calendar timezone: {str(e)}")
            return 'UTC'

class EmailProcessor:
    def __init__(self):
        self.gmail = GmailClient()
        self.classifier = EmailClassifier()
        self.calendar = CalendarClient()
        self.db = DatabaseService()
        
        profile = self.gmail.service.users().getProfile(userId='me').execute()
        self.user_email = profile['emailAddress']
        self.current_user = self.db.get_user(self.user_email)
        
        if not self.current_user:
            token_data = {}  
            self.current_user = self.db.create_user(self.user_email, token_data)

    def _enter_composition_flow(self, draft: str, recipient: str, subject: str, 
                            thread_id: str = None, context: str = ""):
        current_content = draft
        original_prompt = f"Response to: {subject}"
        
        while True:
            print("\n✉️ Email Composition Menu:")
            print("[1] Send email now")
            print("[2] Edit line-by-line")
            print("[3] Revise with AI instructions")
            print("[4] Regenerate from scratch")
            print("[5] View original message")
            print("[6] Cancel and return")
            
            choice = input("Select option: ").strip()
            
            if choice == "1": 
                print(f"\nTo: {recipient}")
                print(f"Subject: {subject}")
                print("\nMessage Content:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                
                confirm = input("Send this email? (y/n): ").lower()
                if confirm == 'y':
                    result = self.gmail.send_email(
                        to=recipient,
                        subject=subject,
                        body=current_content,
                        thread_id=thread_id
                    )
                    if "Message Id" in result:
                        message_id = result.split(": ")[1] if ": " in result else "unknown"
                        self.db.log_sent_email({
                        'user_id': self.current_user.id,
                        'thread_id': thread_id,
                        'message_id': message_id,
                        'recipient': recipient,
                        'subject': subject,
                        'body': current_content,
                        'context': context[:1000]  
                    })
                        print("✅ Email sent successfully!")
                        return True
                return False
            
            elif choice == "2": 
                current_content = self._edit_email_interactive(current_content)
                print("\nEdited Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                
            elif choice == "3": 
                print("\nCurrent AI Context:")
                print(f"Original message: {context[:200]}...")
                instruction = input("Enter revision instructions: ")
                current_content = self._edit_email_with_ai(current_content, instruction)
                print("\nRevised Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                
            elif choice == "4":  
                new_prompt = input("Enter new instructions (or press Enter to keep context): ")
                if not new_prompt:
                    new_prompt = f"Improve this email draft: {current_content[:500]}"
                current_content = self.classifier._call_ai_api(new_prompt)
                current_content = self._clean_generated_email(current_content)
                print("\nRegenerated Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                
            elif choice == "5":  
                print("\nOriginal Message:")
                print("=" * 50)
                print(context)
                print("=" * 50)
                
            elif choice == "6":  
                print("Email composition cancelled.")
                return False
                
            else:
                print("Invalid option. Please choose 1-6.")
    def _compose_response_for_email(self, email: Dict[str, str]):
        if not email or not email.get('body'):
            print("⚠️ Cannot compose response - no email content")
            return
            
        print("\n✉️ Composing response...")
        
        sender = email.get('from', '')
        subject = email.get('subject', 'Response to your email')
        body = email.get('body', '')
        
        sender_email = ""
        if sender:
            email_match = re.search(r'<([^>]+)>', sender)
            sender_email = email_match.group(1) if email_match else sender
        
        prompt = f"""
        Compose a professional email response to this message:
        
        From: {sender}
        Subject: {subject}
        
        Original Message:
        {body[:1000]}
        
        Response should:
        - Be polite and professional
        - Address all points in the original email
        - Keep it concise (3-5 sentences max)
        - Include a proper greeting and closing
        """
        
        draft = self.classifier._call_ai_api(prompt)
        draft = self._clean_generated_email(draft)
        
        print("\nAI-Generated Draft:")
        print("=" * 50)
        print(draft)
        print("=" * 50)
        
        self._enter_composition_flow(
            draft=draft,
            recipient=sender_email,
            subject=f"Re: {subject}",
            thread_id=email.get('threadId'),
            context=body
        )
    def process_inbox(self, lookback_hours: int = 24):
        try:
            print("\n=== Processing Inbox ===")
            cutoff = datetime.now(pytz.utc) - timedelta(hours=lookback_hours)
            print(f"Looking for emails since {cutoff}")
            
            emails = self.gmail.get_recent_emails(max_results=50, after_date=cutoff.isoformat())
            
            if not emails:
                print("No recent emails found to process.")
                input("Press Enter to return to main menu...")
                return
                
            print(f"Found {len(emails)} emails to process...\n")
            
            current_index = 0
            while current_index < len(emails):
                email = emails[current_index]
                
                if self.db.email_already_processed(email['id']):
                    print(f"Email already processed: {email['subject']}")
                    current_index += 1
                    continue
                    
                full_email = self.gmail.get_email_content(email['id'])
                
                if not full_email or not full_email.get('body'):
                    print(f"Skipping email - couldn't retrieve content: {email['subject']}")
                    current_index += 1
                    continue
                
                print("="*80)
                print(f"EMAIL {current_index+1}/{len(emails)}")
                print(f"From: {email['from']}")
                print(f"Subject: {email['subject']}")
                print(f"Snippet: {email['snippet'][:200]}{'...' if len(email['snippet']) > 200 else ''}")
                print("="*80)
                
                classification = self.classifier.classify_email(full_email.get('body', ''))
                actions = {"processed": True}
                meeting_actions = {}
                
                if self._is_meeting_request(full_email.get('body', '')):
                    print("🔔 Meeting request detected!")
                    meeting_actions = self._handle_meeting_email(full_email)
                    actions.update(meeting_actions)
                else:
                    print("ℹ️ No meeting request detected")
                
                self.db.log_processed_email({
                    'id': full_email['id'],
                    'user_id': self.current_user.id,
                    'thread_id': full_email.get('threadId'),
                    'subject': full_email.get('subject', 'No Subject'),
                    'from': full_email.get('from'),
                    'category': classification['category'],
                    'actions': actions,
                    'ai_response': meeting_actions.get('ai_response', '') 
                })
                
                while True:
                    print("\nActions for this email:")
                    print("[C] Compose response")
                    print("[N] Next email  [P] Previous email  [Q] Quit to menu")
                    choice = input("Choose action: ").lower().strip()
                    
                    if choice == 'c':
                        self._compose_response_for_email(full_email)
                    elif choice == 'n':
                        current_index += 1
                        break  
                    elif choice == 'p':
                        current_index = max(0, current_index - 1)
                        break  
                    elif choice == 'q':
                        print("\nExiting inbox processing...")
                        input("Press Enter to return to main menu...")
                        return 
                    else:
                        print("Invalid choice. Please choose C, N, P, or Q.")
            
            print(f"\nProcessed {current_index} emails.")
            
        except Exception as e:
            print(f"Error processing inbox: {str(e)}")
            import traceback
            traceback.print_exc() 
        finally:
            input("Press Enter to return to main menu...")
    def _select_existing_event(self, days_ahead=7):
        """Let user select an existing event to reschedule"""
        now = datetime.now(DUBAI_TIMEZONE)
        end_date = now + timedelta(days=days_ahead)
        
        print(f"\n📅 Listing events in the next {days_ahead} days:")
        events = self.calendar.list_events()
        
        if not events:
            print("No upcoming events found")
            return None
            
        filtered_events = []
        for event in events:
            start_str = event['start'].get('dateTime', event['start'].get('date'))
            start = parser.parse(start_str)
            if now <= start <= end_date:
                filtered_events.append(event)
                
        if not filtered_events:
            print("No events found in the time range")
            return None
            
        for i, event in enumerate(filtered_events):
            start_str = event['start'].get('dateTime', event['start'].get('date'))
            start = parser.parse(start_str)
            print(f"[{i}] {event['summary']} - {start.strftime('%a %b %d, %I:%M %p')}")
            
        choice = input("Select event number (or 'c' to cancel): ").strip()
        if choice.lower() == 'c':
            return None
            
        try:
            index = int(choice)
            if 0 <= index < len(filtered_events):
                return filtered_events[index]['id']  
        except ValueError:
            pass
            
        print("Invalid selection")
        return None
    
    def _extract_latest_message(self, email_body: str) -> str:
        """
        Extract only the latest message from an email thread by:
        1. Removing quoted/replied sections
        2. Removing email signatures
        3. Isolating the user's new text
        """
        quote_patterns = [
            r"On .* wrote:",  # English
            r"Le .* \u00E9crit :",  # French
            r"El .* escribi\u00F3:",  # Spanish
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} .* <.*>:",  # Email headers
            r"From: .*",  # Email headers
            r"-----Original Message-----",
            r"_{10,}",  # Lines of underscores
            r"\-{10,}",  # Lines of dashes
            r"Sent from my .*",  # Signatures
        ]
        
        lines = email_body.splitlines()
        clean_lines = []
        
        for line in lines:
            if any(re.search(pattern, line, re.IGNORECASE) for pattern in quote_patterns):
                break  
                
            if line.strip() and not line.startswith(('>', '|')):
                clean_lines.append(line)
        
        clean_body = "\n".join(clean_lines)
        
        if len(clean_body) < 50:
            match = re.search(r"^(.*?)(?:" + "|".join(quote_patterns) + ")", 
                            email_body, re.DOTALL | re.IGNORECASE)
            if match:
                clean_body = match.group(1).strip()
        
        return clean_body.strip()
    
    def _handle_meeting_email(self, email: Dict[str, str]):
        raw_body = email.get('body', '')
        
        clean_body = self._extract_latest_message(raw_body)
        clean_body_lower = clean_body.lower()
        

        sender = email.get('from')
        subject = email.get('subject', 'Meeting Request')
        actions = {"meeting_processed": True}
        
        # Extract clean sender email
        email_match = re.search(r'<([^>]+)>', sender)
        sender_email = email_match.group(1) if email_match else sender
        
        reschedule_keywords = [
            'reschedule', 're-schedule', 'rearrange', 'change time', 
            'move', 'postpone', 'new time', 'different time', 'adjust',
            'push back', 'push forward', 'shift', 'relocate', 'change our meeting',
            'alternate time', 'rescheduling', 'replan', 're-book'
        ]
        
        is_reschedule = any(keyword in clean_body_lower for keyword in reschedule_keywords)
        
        if not is_reschedule:
            subject_lower = subject.lower()
            is_reschedule = any(keyword in subject_lower for keyword in reschedule_keywords)
        
        thread_id = email.get('threadId')
        
        existing_event = None
        if is_reschedule and thread_id:
            print("🔁 Reschedule request detected")
            existing_event = self.db.get_calendar_event_by_thread(thread_id)
            
            if existing_event:
                print(f"  Found existing event: {existing_event.title} on {existing_event.start_time}")
            else:
                print("  No existing event found for this thread")
                print("\nWould you like to:")
                print("[1] Create a new event")
                print("[2] Choose an existing event to reschedule")
                choice = input("Select option: ").strip()
                
                if choice == "2":
                    existing_event = self._select_existing_event()
                    if not existing_event:
                        print("No event selected. Creating new event instead.")
        else:
            print("  Not a reschedule request")
        
        detection_methods = [
            self._detect_with_nlp,
            self._detect_with_ai,
            self._detect_manual_fallback
        ]
        
        proposed_time = None
        for method in detection_methods:
            proposed_time = method(clean_body) 
            if proposed_time:
                break
                
        if not proposed_time:
            print("Could not detect meeting time")
            if input("Propose a time manually? (y/n): ").lower() == 'y':
                self._propose_new_time(email, sender_email)
            return actions
            
        dubai_time = proposed_time.astimezone(DUBAI_TIMEZONE)
        time_str = dubai_time.strftime('%A, %B %d at %I:%M %p')
        print(f"⏱️ Proposed time: {time_str} (Dubai time)")
        
        confirm = input("Is this correct? (y/n): ").lower()
        if confirm != 'y':
            try:
                new_time = input("Enter correct time (YYYY-MM-DD HH:MM): ")
                manual_time = datetime.strptime(new_time, "%Y-%m-%d %H:%M")
                proposed_time = DUBAI_TIMEZONE.localize(manual_time)
                print(f"Using manual time: {proposed_time.strftime('%A, %B %d at %I:%M %p')}")
            except ValueError:
                print(" Invalid format. Using detected time.")
        

        if self._is_time_available(proposed_time):
            print("This time is available")
            if input("Add to calendar? (y/n): ").lower() == 'y':
                self._schedule_and_confirm(
                    subject, 
                    raw_body,  
                    sender_email, 
                    proposed_time, 
                    email_id=email['id'],
                    existing_event=existing_event
                )
        else:
            print("You're busy at that time")
            if input("Suggest alternative? (y/n): ").lower() == 'y':
                self._suggest_alternative_times(email, sender_email, proposed_time)
        return actions
    def _detect_with_nlp(self, text: str) -> Optional[datetime]:
        try:
            # Get current time in Dubai

            now = datetime.now(DUBAI_TIMEZONE)
            print(f"Current Dubai time: {now.strftime('%Y-%m-%d %H:%M')}")
            clean_text = self._extract_latest_message(text)
            print(f"🧹 Cleaned text for NLP analysis:\n{clean_text}\n{'='*50}")
            patterns = [
                r'tomorrow at (\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',  # "tomorrow at 10pm"
                r'at (\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*tomorrow',  # "at 10pm tomorrow"
                r'next (\w+day)\s*at (\d{1,2}(?::\d{2})?\s*(?:am|pm)?)'  # "next friday at 8pm"
            ]
            
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    time_str = ' '.join(match.groups())
                    print(f"Pattern matched: '{pattern}' → Extracted: '{time_str}'")
                    
                    parsed = dateparser.parse(
                        time_str,
                        settings={
                            'TIMEZONE': 'Asia/Dubai',
                            'RELATIVE_BASE': now,
                            'PREFER_DATES_FROM': 'future',
                        },
                        languages=['en'] 
                    )

                    if parsed:
                        if not parsed.tzinfo:
                            parsed = DUBAI_TIMEZONE.localize(parsed)
                        
                        print(f"Parsed time: {parsed.strftime('%Y-%m-%d %H:%M')}")
                        
                        if parsed < now:
                            print("⚠️ Parsed time is in the past! Ignoring.")
                            return None
                            
                        return parsed
            return None
        except Exception as e:
            print(f"⚠️ NLP detection error: {str(e)}")
            return None
    def _detect_with_ai(self, text: str) -> Optional[datetime]:
        try:
            clean_text = self._extract_latest_message(text)
            now = datetime.now(DUBAI_TIMEZONE)
            print(f"AI detection using current time: {now}")

            prompt = f'''Extract meeting time ONLY from the MOST RECENT message in this email. 
            Ignore any quoted/forwarded content or previous messages. Respond ONLY with:
            - ISO 8601 format (YYYY-MM-DDTHH:MM:SS) in Asia/Dubai timezone
            - "none" if no time found in the new message
            
            Current Date: {datetime.now(DUBAI_TIMEZONE).date()}
            Email: {clean_text[:2000]}'''
            
            response = self.classifier._call_ai_api(prompt).strip()
            print(f"AI response: {response}")
            
            if response.lower() != 'none':
                if 'T' not in response:
                    response = re.sub(r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})', r'\1T\2', response)
                
                dt = parser.parse(response)
                
                if dt.tzinfo is None:
                    dt = DUBAI_TIMEZONE.localize(dt)
                    
                if dt < now:
                    print("⚠️ AI returned past date!")
                    return None
                    
                return dt
        except Exception as e:
            print(f"⚠️ AI detection failed: {str(e)}")
            return self._detect_manual_fallback(text)
        return None

    def _detect_manual_fallback(self, text: str) -> Optional[datetime]:
        try:
            time_match = re.search(r'(\d{1,2}:\d{2}\s*(?:am|pm)?', text, re.IGNORECASE)
            day_match = re.search(r'(mon|tue|wed|thu|fri|sat|sun)', text, re.IGNORECASE)
            clean_text = self._extract_latest_message(text)
            print(f"🧹 Cleaned text for NLP analysis:\n{clean_text}\n{'='*50}")
            if time_match and day_match:
                time_str = time_match.group(1)
                day_str = day_match.group(1)
                combined = f"next {day_str} at {time_str}"
                parsed = dateparser.parse(combined, settings={'TIMEZONE': 'Asia/Dubai'})
                if parsed:
                    return DUBAI_TIMEZONE.localize(parsed)
        except Exception:
            pass
        return None

    def _is_meeting_request(self, body: str) -> bool:
        clean_body = self._extract_latest_message(body)
        clean_body_lower = clean_body.lower()
        meeting_phrases = [
            # Core meeting terms
            "meet", "meeting", "gathering", "appointment", "session", "conference", "consultation",
            
            # Scheduling terms
            "schedule", "book a", "set up", "arrange", "plan a", "organize", "coordinate",
            
            # Time-specific terms
            "catch up", "touch base", "sync up", "check in", "follow up", "get together", "reconnect",
            
            # Activity-based terms
            "grab coffee", "lunch meeting", "dinner meeting", "video call", "zoom call", "teams meeting", 
            "google meet", "call", "chat", "discuss", "talk", "brainstorm", "review", "briefing", 
            "workshop", "presentation", "demo", "walkthrough", 
            
            # Confirmation terms
            "confirm our", "still on", "are we still", "following up", "checking in", "reminder about",
            "as agreed", "as discussed", "as planned",
            
            # Location-based terms
            "in person", "face to face", "at the office", "remotely", "virtually",
            
            # Invitation terms
            "invite you", "join us", "would you be available", "are you free", "are you available",
            "let's connect", "can we", "could we", "would you like to", "suggest we",
            
            # Formal terms
            "interview", "negotiation", "mediation", "assessment", "evaluation", "training", "onboarding"
        ]
        
        # Common false positives to exclude
        exclude_phrases = [
            "meeting room", "meeting rooms", "meeting point", "meeting place", "meeting link",
            "meeting id", "meeting password", "meeting agenda", "meeting minutes", "meeting notes",
            "meeting recording", "meeting schedule", "meeting request", "meeting invitation"
        ]
        
        body_lower = body.lower()
        
        if any(phrase in body_lower for phrase in exclude_phrases):
            return False
            
        return any(phrase in body_lower for phrase in meeting_phrases)



    def _is_time_available(self, start_time: datetime, duration_minutes: int = 60) -> bool:
        end_time = start_time + timedelta(minutes=duration_minutes)
        return self.calendar.check_availability(start_time, end_time)
    def extract_event_id(event_link):
        parsed = urlparse(event_link)
        query = parse_qs(parsed.query)
        return query.get('eid', [None])[0]
    
    def _schedule_and_confirm(self, subject, body, sender, start_time, email_id, existing_event=None):
        dubai_time = start_time.astimezone(DUBAI_TIMEZONE)
        end_time = start_time + timedelta(hours=1)
        time_str = dubai_time.strftime('%A, %B %d at %I:%M %p')
        
        meeting_summary = self.classifier.summarize_meeting(body)
        event_created = False

        if isinstance(existing_event, str):
            event_id = existing_event
            event_result = self.calendar.update_event(
                event_id=event_id,
                new_start_time=start_time,
                new_end_time=end_time,
                description=f"Rescheduled meeting:\n{meeting_summary}"
            )
            if event_result:
                print(f"🔁 Rescheduled manually selected event: {event_result.get('link', '')}")
                db_event = self.db.get_calendar_event_by_google_id(event_id)
                if db_event:
                    self.db.update_calendar_event(
                        event_id=db_event.id,
                        new_start_time=start_time,
                        new_end_time=end_time,
                        new_description=f"Rescheduled: {meeting_summary}"
                    )
                event_created = True

        elif existing_event: 
            event_id = existing_event.google_event_id
            event_result = self.calendar.update_event(
                event_id=event_id,
                new_start_time=start_time,
                new_end_time=end_time,
                description=f"Rescheduled: {meeting_summary}"
            )
            if event_result:
                print(f"🔁 Rescheduled existing event: {event_result.get('link', '')}")
                self.db.update_calendar_event(
                    event_id=existing_event.id,
                    new_start_time=start_time,
                    new_end_time=end_time,
                    new_description=f"Rescheduled: {meeting_summary}"
                )
                event_created = True

        else:  
            event_result = self.calendar.create_event(
                summary=f"Meeting: {subject}",
                start_time=start_time,
                end_time=end_time,
                attendees=[sender],
                description=f"Automatically scheduled:\n\n{meeting_summary}"
            )
            if event_result:
                print(f"✅ New meeting scheduled: {event_result.get('link', '')}")
                self.db.log_calendar_event({
                    'user_id': self.current_user.id,
                    'email_id': email_id,
                    'title': f"Meeting: {subject}",
                    'start_time': start_time,
                    'end_time': end_time,
                    'attendees': [sender],
                    'google_event_id': event_result['id'],
                    'description': meeting_summary
                })
                event_created = True

        if event_created:
            confirm_body = (
                f"Hi there,\n\nI've scheduled our meeting for {time_str} (Dubai time)."
                f"\n\nLooking forward to our conversation!\n\nBest regards,\n[Your Name]"
            )
            result = self.gmail.send_email(
                to=sender,
                subject=f"Confirmed: {subject}",
                body=confirm_body,
                thread_id=email_id
            )
            
            if result.get('status') == 'success':
                self.db.log_sent_email({
                    'user_id': self.current_user.id,
                    'thread_id': email_id,
                    'message_id': result.get('message_id'),
                    'recipient': sender,
                    'subject': f"Confirmed: {subject}",
                    'body': confirm_body,
                    'context': f"Meeting confirmation for {time_str}"
                })
                print("✅ Confirmation email sent and logged!")
                return True
        
        print("❌ Failed to schedule/confirm meeting")
        return False
    def _suggest_alternative_times(self, email, sender, original_time):
        print("  🔍 Finding available times near proposed time...")
        alternatives = self._find_available_times(original_time)
        
        if not alternatives:
            print("  ⚠️ No good alternatives found nearby")
            return self._propose_new_time(email, sender)
        
        response_body = self._generate_alternative_time_response(
            email.get('body', ''),
            original_time,
            alternatives
        )
        
        print("\n📝 Suggested Response:")
        print("=" * 50)
        print(response_body)
        print("=" * 50)
        
        send = input("Send this response? (y/n): ").lower()
        if send == 'y':
            subject = f"Re: {email.get('subject', 'Meeting Request')}"
            result = self.gmail.send_email(
                to=sender,
                subject=subject,
                body=response_body
            )
            if "Message Id" in result:
                print("  ✅ Response sent successfully!")
                return True
        return False

    def _find_available_times(self, original_time, max_results=3, search_range=7):
        alternatives = []
        time_slots = [
            original_time + timedelta(hours=1),
            original_time - timedelta(hours=1),
            original_time + timedelta(days=1),
            original_time - timedelta(days=1),
            original_time + timedelta(weeks=1)
        ]
        
        for i in range(1, search_range + 1):
            time_slots.append(original_time + timedelta(days=i))
        
        for slot in time_slots:
            if self._is_time_available(slot):
                alternatives.append(slot)
                if len(alternatives) >= max_results:
                    break
        
        return alternatives

    def _generate_alternative_time_response(self, body, original_time, alternatives):
        original_str = original_time.strftime('%A, %B %d at %I:%M %p')
        alt_str = "\n".join([f"- {t.strftime('%A, %B %d at %I:%M %p')}" for t in alternatives])
        
        prompt = (
            "Compose a polite email response suggesting alternative meeting times. "
            "Keep it professional but friendly. Reference the original meeting request "
            "and explain that the proposed time is unavailable. Suggest the alternative "
            "times clearly and ask which works best.\n\n"
            f"Original proposed time: {original_str}\n"
            f"Available times:\n{alt_str}\n\n"
            f"Original message context: {body[:300]}"
        )
        
        return self.classifier._call_ai_api(prompt)

    def _propose_new_time(self, email, sender):
        """Propose a new meeting time when none is specified"""
        print("  ⌚ Finding next available time slot...")
        now = datetime.now(pytz.utc)
        next_available = None
        
        for day in range(0, 7):
            for hour in [9, 11, 14, 16]: 
                candidate = now + timedelta(days=day, hours=hour)
                if self._is_time_available(candidate):
                    next_available = candidate
                    break
            if next_available:
                break
        
        if not next_available:
            print("  ⚠️ No available times found in next 7 days")
            return False
        
        print(f"  ⏱️ Next available time: {next_available.strftime('%A, %B %d at %I:%M %p')}")
        choice = input("  Propose this time? (y/n): ").lower()
        
        if choice == 'y':
            proposal_body = (
                f"Hi there,\n\n"
                f"Thanks for your meeting request! How about "
                f"{next_available.strftime('%A, %B %d at %I:%M %p')}?\n\n"
                f"Please let me know if this works for you.\n\n"
                f"Best regards,\n[Your Name]"
            )
            
            result = self.gmail.send_email(
                to=sender,
                subject=f"Re: {email.get('subject', 'Meeting')}",
                body=proposal_body
            )
            
            if "Message Id" in result:
                print("  ✅ Time proposal sent successfully!")
                return True
        return False

    def _clean_generated_email(self, content: str) -> str:
        prefixes = [
            "Here's a polished and professional email you could use",
            "Below is a professional email template",
            "Here is a professional email draft",
            "Certainly! Below is a polite and professional email template",
            "Here’s a polished and professional email",
            "Here's a professional email draft",
            "Here's a polished and professional email you could use"
        ]
        
        for prefix in prefixes:
            if content.startswith(prefix):
                content = content[len(prefix):].lstrip(": \n-")
        
        content = re.sub(r"\*{2}Subject:\*{2}\s*", "Subject: ", content)
        content = re.sub(r"\*{2}(.*?)\*{2}", r"\1", content)
        content = re.sub(r"#{2,}\s*(.*?)\s*", "", content)
        content = re.sub(r"---.*", "", content, flags=re.DOTALL)
        content = re.sub(r"###.*", "", content, flags=re.DOTALL)
        
        footer_phrases = [
            "Optional Additions:",
            "Customization Tips:",
            "Notes:",
            "Adjust based on",
            "Let me know if you'd like any adjustments",
            "Feel free to customize",
            "You can adjust"
        ]
        for phrase in footer_phrases:
            if phrase in content:
                content = content.split(phrase)[0]
                
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        return content.strip()
    
    def compose_email(self, query: str):
        try:
            print("\n✉️ Starting New Email Composition")
            draft = self.classifier._call_ai_api(f"Write a professional email about: {query}")
            draft = self._clean_generated_email(draft)
            
            print("\nGenerated Draft:")
            print("=" * 50)
            print(draft)
            print("=" * 50)
            
            recipient = input("Recipient email: ").strip()
            subject = input("Subject: ").strip()
            
            self._enter_composition_flow(
                draft=draft,
                recipient=recipient,
                subject=subject,
                context=query
            )
            
        except Exception as e:
            print(f"Error composing email: {str(e)}")
        
    def _edit_email_interactive(self, content: str) -> str:
        print("\n✏️ Line Editor Mode (press Enter to keep line, type new text to edit)")
        print("Type '!exit' to finish editing, '!skip' to keep the rest as-is")
        lines = content.split('\n')
        edited_lines = []
        
        for i, line in enumerate(lines):
            new_line = input(f"Line {i+1}/{len(lines)}:\nOriginal: {line}\nEdit: ")
            
            if new_line == '!exit':
                edited_lines.extend(lines[i:])
                break
            elif new_line == '!skip':
                edited_lines.append(line)
                edited_lines.extend(lines[i+1:])
                break
            elif new_line == '':
                edited_lines.append(line)
            else:
                edited_lines.append(new_line)
        
        return '\n'.join(edited_lines)
        
    def _edit_email_with_ai(self, content: str, instruction: str) -> str:
        prompt = (
            f"Revise the following email based on these instructions: {instruction}\n\n"
            f"Original email:\n{content}\n\n"
            "Revised email (without any additional explanations or formatting):"
        )
        revised_content = self.classifier._call_ai_api(prompt)
        return self._clean_generated_email(revised_content)
        
    def _present_editing_menu(self, email_content: str, original_query: str):
        current_content = email_content
        while True:
            print("\nChoose an option:")
            print("[1] Send email")
            print("[2] Edit line-by-line")
            print("[3] Revise with AI instructions")
            print("[4] Regenerate from scratch")
            print("[5] Cancel and return to menu")
            choice = input("Select option: ").strip()
                
            if choice == "1":  
                to = input("Recipient email: ")
                subject = input("Subject: ")
                result = self.gmail.send_email(to, subject, current_content)
                if "Message Id" in result:
                    print("Email sent successfully!")
                else:
                    print(f"Failed to send email: {result}")
                return
                    
            elif choice == "2":  
                current_content = self._edit_email_interactive(current_content)
                print("\nEdited Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                    
            elif choice == "3":  
                instruction = input("Enter revision instructions (e.g., 'make it casual', 'change time to 6pm'): ")
                current_content = self._edit_email_with_ai(current_content, instruction)
                print("\nRevised Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                    
            elif choice == "4":  
                new_prompt = input("Enter new instructions (or press Enter to keep original): ") or original_query
                new_content = self.classifier._call_ai_api(f"Write a professional email about: {new_prompt}")
                current_content = self._clean_generated_email(new_content)
                print("\nRegenerated Email:")
                print("=" * 50)
                print(current_content)
                print("=" * 50)
                    
            elif choice == "5":  
                print("Email composition cancelled.")
                return
                    
            else:
                print("Invalid option. Please choose 1-5.")    


    def _detect_meeting_time(self, text: str) -> datetime:
        try:
            email_date_match = re.search(r'(\d{1,2} [а-я]+\. \d{4} г\. в \d{1,2}:\d{2})', text)
            if email_date_match:
                email_date_str = email_date_match.group(1)

                email_date = dateparser.parse(
                    email_date_str, 
                    languages=['ru'],
                    settings={'TIMEZONE': 'Asia/Dubai'}
                )
            else:
                email_date = datetime.now(DUBAI_TIMEZONE)
            
            body_match = re.search(r'hey Kristina! (.+)', text)
            if body_match:
                body_text = body_match.group(1)
            else:
                body_text = text
                
            prompt = (
                f"Email was sent on: {email_date.strftime('%Y-%m-%d %H:%M')} Dubai time\n"
                f"Email content: {body_text}\n\n"
                "What is the exact meeting date and time mentioned? "
                "Respond ONLY in ISO 8601 format (YYYY-MM-DDTHH:MM:SS) for Asia/Dubai timezone. "
                "If no time is mentioned, return 'none'."
            )
            
            print("  Using AI to detect meeting time...")
            response = self.classifier._call_ai_api(prompt).strip()
            
            clean_response = re.sub(r'[^0-9T:\-]', '', response)
            if clean_response and clean_response.lower() != 'none':
                dt = parser.isoparse(clean_response)
                if not dt.tzinfo:
                    dt = DUBAI_TIMEZONE.localize(dt)
                return dt
        except Exception as e:
            print(f"AI detection failed: {str(e)}")
        
        return None
    def _is_time_available(self, start_time: datetime, duration_minutes: int = 60) -> bool:
        end_time = start_time + timedelta(minutes=duration_minutes)
        return self.calendar.check_availability(start_time, end_time)

    def _schedule_and_respond(self, subject, body, sender, start_time, response_message):
        end_time = start_time + timedelta(hours=1)
        
        calendar = CalendarClient()
        event_link = calendar.create_event(
            summary=f"Meeting: {subject}",
            start_time=start_time,
            end_time=end_time,
            attendees=[sender],
            description=f"Automatically scheduled from email:\n\n{body[:500]}"
        )
        
        if event_link:
            print(f"✅ Meeting scheduled: {event_link}")
            
            subject = f"Confirmed: {subject}"
            result = self.gmail.send_email(
                to=sender,
                subject=subject,
                body=response_message
            )
            
            if "Message Id" in result:
                print("✅ Confirmation sent successfully!")
                return True
        return False

    def _suggest_alternative_times(self, email, sender, original_time):
        print("🔍 Finding available times...")
        alternatives = self._find_available_times(original_time)
        
        if not alternatives:
            print("⚠️ No available times found")
            return self._propose_new_time(email, sender)
        
        response_body = self._generate_alternative_time_response(
            email.get('body', ''),
            original_time,
            alternatives
        )
        
        print("\n✉️ Response Draft:")
        print("=" * 50)
        print(response_body)
        print("=" * 50)
        
        send = input("Send this response? (y/n): ").lower()
        if send == 'y':
            subject = f"Re: {email.get('subject', 'Meeting Request')}"
            result = self.gmail.send_email(
                to=sender,
                subject=subject,
                body=response_body
            )
            if "Message Id" in result:
                print("✅ Response sent successfully!")
                return True
        return False

    def _find_available_times(self, original_time, max_results=3):
        alternatives = []
        time_slots = [
            original_time + timedelta(hours=1),
            original_time - timedelta(hours=1),
            original_time + timedelta(days=1, hours=original_time.hour),
            original_time - timedelta(days=1),
            original_time + timedelta(weeks=1)
        ]
        
        for slot in time_slots:
            if self._is_time_available(slot):
                alternatives.append(slot)
                if len(alternatives) >= max_results:
                    break
        
        return alternatives

    def _generate_alternative_time_response(self, body, original_time, alternatives):
        original_str = original_time.strftime('%A, %B %d at %I:%M %p')
        alt_str = "\n".join([f"- {t.strftime('%A, %B %d at %I:%M %p')}" for t in alternatives])
        
        prompt = (
            f"Compose a polite email response suggesting alternative meeting times. "
            f"Original proposed time was: {original_str}\n"
            f"Available times:\n{alt_str}\n\n"
            f"Reference the original message: {body[:300]}\n\n"
            "Response should be professional and include all alternative times."
        )
        
        return self.classifier._call_ai_api(prompt)

    def _propose_new_time(self, email, sender):
        print("⌚ Finding next available time...")
        now = datetime.now(pytz.utc)
        next_available = None
        
        for day in range(0, 7):
            for hour in [9, 11, 14, 16]:
                candidate = now + timedelta(days=day, hours=hour)
                if self._is_time_available(candidate):
                    next_available = candidate
                    break
            if next_available:
                break
        
        if not next_available:
            print("⚠️ No available times found in next 7 days")
            return False
        
        print(f"Next available time: {next_available.strftime('%A, %B %d at %I:%M %p')}")
        response = input("Propose this time? (y/n): ").lower()
        
        if response == 'y':
            return self._schedule_and_respond(
                email.get('subject', 'Meeting'),
                email.get('body', ''),
                sender,
                next_available,
                f"Thanks for your invitation! How about {next_available.strftime('%A, %B %d at %I:%M %p')}?"
            )
        return False

    def _schedule_meeting(self, default_attendees=None, default_summary=""):
        if default_attendees is None:
            default_attendees = []
        try:
            print("\n📅 Schedule a Meeting")
            print("="*50)
            
            while True:
                summary = input(f"Meeting title [{default_summary}]: ").strip() or default_summary
                if summary:
                    break
                print("❌ Meeting title cannot be empty. Please enter a title.")

            location = input("Location (optional): ").strip()
            
            attendees = []
            print("\nEnter attendee emails (one per line). Type 'done' when finished:")
            
            if default_attendees:
                print(f"Default attendees: {', '.join(default_attendees)}")
                use_default = input("Use these attendees? (y/n): ").strip().lower()
                if use_default == 'y':
                    attendees = default_attendees
            
            if not attendees:
                print("Add at least one attendee:")
                attendee_count = 0
                while True:
                    attendee = input(f"Attendee #{len(attendees)+1} email: ").strip()
                    
                    if attendee.lower() == 'done':
                        if attendees:
                            break
                        print("❌ You must add at least one attendee.")
                        continue
                        
                    if not '@' in attendee:
                        print("❌ Invalid email format. Must contain '@'. Example: user@example.com")
                        continue
                        
                    attendees.append(attendee)
                    print(f"✓ Added {attendee}")
                    
                    if len(attendees) > 0:
                        another = input("Add another? (y/n): ").lower().strip()
                        if another != 'y':
                            break
            
            while True:
                start_str = input("Start time (YYYY-MM-DD HH:MM): ").strip()
                try:
                    start_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M")
                    break
                except ValueError:
                    print("❌ Invalid format. Please use YYYY-MM-DD HH:MM format. Example: 2025-08-15 14:30")
            
            while True:
                duration_str = input("Duration in minutes: ").strip()
                try:
                    duration = int(duration_str)
                    if duration <= 0:
                        print("❌ Duration must be a positive number.")
                        continue
                    end_time = start_time + timedelta(minutes=duration)
                    break
                except ValueError:
                    print("❌ Please enter a valid number (e.g., 30, 60, 90).")
            
            start_time = DUBAI_TIMEZONE.localize(start_time)
            end_time = DUBAI_TIMEZONE.localize(end_time)
            
            print("\n📝 Meeting Details:")
            print(f"Title: {summary}")
            print(f"Time: {start_time.strftime('%A, %B %d at %I:%M %p')} to {end_time.strftime('%I:%M %p')}")
            print(f"Duration: {duration} minutes")
            print(f"Attendees: {', '.join(attendees)}")
            if location:
                print(f"Location: {location}")
            
            confirm = input("\nSchedule this meeting? (y/n): ").lower().strip()
            if confirm != 'y':
                print("❌ Meeting scheduling cancelled.")
                return False
            
            calendar = CalendarClient()
            event_link = calendar.create_event(
                summary=summary,
                start_time=start_time,
                end_time=end_time,
                attendees=attendees,
                location=location or None
            )
            
            if event_link:
                print(f"✅ Meeting scheduled: {event_link}")
                return True
            print("❌ Failed to schedule meeting")
            return False
            
        except Exception as e:
            print(f"❌ Error scheduling meeting: {str(e)}")
            return False 

class CLIInterface:
    def __init__(self):
        try:
            self.email_processor = EmailProcessor()
            self.calendar_client = CalendarClient()
        except Exception as e:
            print(f"❌ Failed to initialize: {str(e)}")
            self.email_processor = None
            self.calendar_client = None

    def show_menu(self):
        if not self.email_processor or not self.calendar_client:
            print("Cannot start - authentication failed")
            return
            
        print("\nTesting connections...")
        self.email_processor.gmail.test_gmail_connection()
        self.calendar_client.test_connection()
        input("\nPress Enter to continue to main menu...")
        
        while True:
            try:
                print("\nSMART EMAIL ASSISTANT")
                print("1. Compose Email")
                print("2. Process Inbox")
                print("3. Schedule Meeting")
                print("4. View Upcoming Events")
                print("5. Exit")
                
                choice = input("Select option: ").strip()
                
                if choice == "1":
                    self.compose_email()
                elif choice == "2":
                    self.email_processor.process_inbox()
                elif choice == "3":
                    self.email_processor._schedule_meeting(default_attendees=[], default_summary="")
                elif choice == "4":
                    self.view_upcoming_events()
                elif choice == "5":
                    print("Goodbye!")
                    break
                else:
                    print("Invalid option. Please choose 1-5.")
                    
            except KeyboardInterrupt:
                print("\nOperation cancelled. Returning to main menu...")
                continue
            except Exception as e:
                print(f"\nAn error occurred: {str(e)}")
                print("Returning to main menu...")
                continue

    def compose_email(self):
        try:
            user_query = input("Enter email purpose: ").strip()
            if not user_query:
                print("Purpose cannot be empty")
                return
                
            self.email_processor.compose_email(user_query)
            
        except Exception as e:
            print(f"Error composing email: {str(e)}")
    
    def view_upcoming_events(self):
        try:
            events = self.calendar_client.list_events(max_results=10)
            if not events:
                print("\nNo upcoming events found.")
                return
                
            print("\n📅 Upcoming Events:")
            print("=" * 50)
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                print(f"{event['summary']} ({start})")
            print("=" * 50)
            
        except Exception as e:
            print(f"Error viewing events: {str(e)}")
if __name__ == "__main__":
    CLIInterface().show_menu()
    from database import init_db
    init_db()
    CLIInterface().show_menu()
