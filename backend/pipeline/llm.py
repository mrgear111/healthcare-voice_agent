import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

class LLMService:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = "claude-3-haiku-20240307"

    async def get_response(self, messages, tools=None):
        # Convert messages to Anthropic format if needed
        # Anthropic doesn't use 'system' as a role in message list, it's a separate param
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]
        
        # Convert tools to Anthropic format
        anthropic_tools = []
        if tools:
            for tool in tools:
                anthropic_tools.append({
                    "name": tool["function"]["name"],
                    "description": tool["function"]["description"],
                    "input_schema": tool["function"]["parameters"]
                })

        return await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_msg,
            messages=user_messages,
            tools=anthropic_tools,
            stream=True
        )

    async def run_tool(self, tool_name, args, appointment_service):
        if tool_name == "get_available_slots":
            return appointment_service.get_available_slots(args["doctor_id"], args["date"])
        elif tool_name == "book_appointment":
            return appointment_service.book_appointment(
                args["patient_id"], args["doctor_id"], args["date"], args["time"]
            )
        elif tool_name == "cancel_appointment":
            return appointment_service.cancel_appointment(args["appointment_id"])
        return {"error": "Unknown tool"}

    def get_system_prompt(self, patient_context=None, language="en"):
        prompts = {
            "en": """You are Aura, an advanced clinical voice assistant. 
            Your goal is to book, reschedule, or cancel appointments.
            
            CHECKLIST FOR BOOKING:
            1. Patient ID (ask for it if missing)
            2. Doctor (Sharma [d1], Priya [d2], Karthik [d3])
            3. Date (YYYY-MM-DD)
            4. Time (check slots first!)
            
            GUIDELINES:
            - If details are missing, ask for them clearly.
            - Once you have the Doctor and Date, call 'get_available_slots' proactively.
            - Always maintain a professional and empathetic tone.
            - Keep responses concise for voice interaction.""",
            "hi": """आप औरा (Aura) हैं, एक उन्नत नैदानिक आवाज़ सहायक।
            आपका लक्ष्य नियुक्तियों को बुक करना, पुनर्निर्धारित करना या रद्द करना है।
            
            बुकिंग के लिए चेकलिस्ट:
            1. मरीज़ की आईडी (अगर नहीं है तो मांगें)
            2. डॉक्टर (शर्मा [d1], प्रिया [d2], कार्तिक [d3])
            3. तारीख (YYYY-MM-DD)
            4. समय (पहले स्लॉट की जांच करें!)
            
            दिशानिर्देश:
            - विवरण गायब होने पर स्पष्ट रूप से पूछें।
            - डॉक्टर और तारीख मिलते ही 'get_available_slots' को कॉल करें।
            - स्वर पेशेवर और सहानुभूतिपूर्ण रखें।""",
            "ta": """நீங்கள் ஆரா (Aura), ஒரு மேம்பட்ட மருத்துவ குரல் உதவியாளர்.
            சந்திப்புகளை முன்பதிவு செய்யவும், மாற்றியமைக்கவும் அல்லது ரத்து செய்யவும் நோயாளிகளுக்கு உதவுவதே உங்கள் குறிக்கோள்.
            
            முன்பதிவுக்கான சரிபார்ப்புப் பட்டியல்:
            1. நோயாளி ஐடி (இல்லை என்றால் கேட்கவும்)
            2. மருத்துவர் (சர்மா [d1], பிரியா [d2], கார்த்திக் [d3])
            3. தேதி (YYYY-MM-DD)
            4. நேரம் (முதலில் இடங்களைச் சரிபார்க்கவும்!)
            
            வழிகாட்டுதல்கள்:
            - விவரங்கள் இல்லையென்றால் தெளிவாகக் கேட்கவும்.
            - மருத்துவர் மற்றும் தேதி கிடைத்ததும் 'get_available_slots' ஐ அழைக்கவும்.
            - குரல் சுருக்கமாகவும் தெளிவாகவும் இருக்கட்டும்."""
        }
        
        base_prompt = prompts.get(language, prompts["en"])
        if patient_context:
            base_prompt += f"\n\nPatient History: {patient_context}"
            
        return base_prompt
