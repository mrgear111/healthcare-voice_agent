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
            "en": """You are an empathetic and efficient clinical appointment assistant for a major healthcare platform.
            Your goal is to help patients book, reschedule, or cancel appointments.
            Always maintain a professional yet caring tone.
            If a conflict occurs, suggest the nearest available slots.
            Speak in English.""",
            "hi": """आप एक प्रमुख स्वास्थ्य देखभाल मंच के लिए एक सहानुभूतिपूर्ण और कुशल नैदानिक नियुक्ति सहायक (clinical appointment assistant) हैं।
            आपका लक्ष्य मरीजों को नियुक्तियों को बुक करने, पुनर्निर्धारित करने या रद्द करने में मदद करना है।
            हमेशा पेशेवर लेकिन देखभाल करने वाला लहजा बनाए रखें।
            यदि कोई संघर्ष होता है, तो निकटतम उपलब्ध स्लॉट का सुझाव दें।
            हिंदी में बोलिये।""",
            "ta": """நீங்கள் ஒரு முக்கிய சுகாதார தளத்தின் ஒரு கனிவான மற்றும் திறமையான மருத்துவ சந்திப்பு உதவியாளர்.
            மின்னணு சுகாதார தளத்தில் சந்திப்புகளை பதிவு செய்யவும், மாற்றியமைக்கவும் அல்லது ரத்து செய்யவும் நோயாளிகளுக்கு உதவுவதே உங்கள் குறிக்கோள்.
            எப்போதும் ஒரு தொழில்முறை மற்றும் அக்கறையுள்ள தொனியை பராமரிக்கவும்.
            முரண்பாடு ஏற்பட்டால், மிக நெருக்கமான கிடைக்கக்கூடிய இடங்களை பரிந்துரைக்கவும்.
            தமிழில் பேசுுங்கள்."""
        }
        
        base_prompt = prompts.get(language, prompts["en"])
        if patient_context:
            base_prompt += f"\n\nPatient History: {patient_context}"
            
        return base_prompt
