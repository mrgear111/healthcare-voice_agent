from datetime import datetime, timedelta
import uuid

class AppointmentService:
    def __init__(self):
        # In-memory storage for demonstration (will be replaced by DB/Redis)
        self.appointments = []
        self.doctors = [
            {"id": "d1", "name": "Dr. Sharma", "specialty": "General Physician"},
            {"id": "d2", "name": "Dr. Priya", "specialty": "Pediatrician"},
            {"id": "d3", "name": "Dr. Karthik", "specialty": "Cardiologist"}
        ]
        self.working_hours = (9, 17) # 9 AM to 5 PM

    def get_available_slots(self, doctor_id, date_str):
        # Generate 30-min slots
        slots = []
        start_time = datetime.strptime(f"{date_str} {self.working_hours[0]}:00", "%Y-%m-%d %H:%M")
        end_time = datetime.strptime(f"{date_str} {self.working_hours[1]}:00", "%Y-%m-%d %H:%M")
        
        current = start_time
        while current < end_time:
            slot_str = current.strftime("%H:%M")
            # Check if occupied
            is_occupied = any(
                a for a in self.appointments 
                if a["doctor_id"] == doctor_id and a["date"] == date_str and a["time"] == slot_str
            )
            if not is_occupied:
                slots.append(slot_str)
            current += timedelta(minutes=30)
            
        return slots

    def book_appointment(self, patient_id, doctor_id, date_str, time_str):
        # Validation
        appointment_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        if appointment_time < datetime.now():
            return {"success": False, "message": "Cannot book in the past."}
            
        # Check conflict
        conflict = any(
            a for a in self.appointments 
            if a["doctor_id"] == doctor_id and a["date"] == date_str and a["time"] == time_str
        )
        if conflict:
            return {"success": False, "message": "Slot already booked."}
            
        appointment = {
            "id": str(uuid.uuid4()),
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "date": date_str,
            "time": time_str,
            "status": "confirmed"
        }
        self.appointments.append(appointment)
        return {"success": True, "appointment": appointment}

    def cancel_appointment(self, appointment_id):
        for i, a in enumerate(self.appointments):
            if a["id"] == appointment_id:
                self.appointments.pop(i)
                return {"success": True}
        return {"success": False, "message": "Appointment not found."}
