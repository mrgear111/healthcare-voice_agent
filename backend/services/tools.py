def get_appointment_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "get_available_slots",
                "description": "Get available appointment slots for a doctor on a specific date.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "doctor_id": {"type": "string", "enum": ["d1", "d2", "d3"], "description": "The ID of the doctor (d1: Sharma, d2: Priya, d3: Karthik)"},
                        "date": {"type": "string", "description": "The date in YYYY-MM-DD format"}
                    },
                    "required": ["doctor_id", "date"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": "Book a clinical appointment for a patient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "The patient's identifier"},
                        "doctor_id": {"type": "string", "enum": ["d1", "d2", "d3"]},
                        "date": {"type": "string", "description": "YYYY-MM-DD format"},
                        "time": {"type": "string", "description": "HH:MM format (24h)"}
                    },
                    "required": ["patient_id", "doctor_id", "date", "time"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {"type": "string", "description": "The unique ID of the appointment"}
                    },
                    "required": ["appointment_id"]
                }
            }
        }
    ]
