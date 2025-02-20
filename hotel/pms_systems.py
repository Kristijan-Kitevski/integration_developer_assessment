from abc import ABC, abstractmethod
import inspect
import sys
import json
from django.db import transaction
from typing import Optional
import phonenumbers
from phonenumbers import NumberParseException
from datetime import datetime, timedelta
from .models import Language

from hotel.external_api import (
	get_reservations_between_dates,
	get_reservation_details,
	get_guest_details,
	APIError,
)

from hotel.models import Stay, Guest, Hotel


class PMS(ABC):
	"""
	Abstract class for Property Management Systems.
	"""

	def __init__(self):
		pass

	@property
	def name(self):
		longname = self.__class__.__name__
		return longname[4:]

	@abstractmethod
	def clean_webhook_payload(self, payload: str) -> dict:
		"""
		Clean the json payload and return a usable dict.
		"""
		raise NotImplementedError

	@abstractmethod
	def handle_webhook(self, webhook_data: dict) -> bool:
		"""
		This method is called when we receive a webhook from the PMS.
		Handle webhook handles the events and updates relevant models in the database.
		Requirements:
			- Now that the PMS has notified you about an update of a reservation, you need to
				get more details of this reservation. For this, you can use the mock API
				call get_reservation_details(reservation_id).
			- Handle the payload for the correct hotel.
			- Update or create a Stay.
			- Update or create Guest details.
		"""
		raise NotImplementedError

	@abstractmethod
	def update_tomorrows_stays(self) -> bool:
		"""
		This method is called every day at 00:00 to update the stays checking in tomorrow.
		Requirements:
			- Get all stays checking in tomorrow by calling the mock API
				get_reservations_between_dates(checkin_date, checkout_date).
			- Update or create the Stays.
			- Update or create Guest details. Deal with missing and incomplete data yourself
				as you see fit. Deal with the Language yourself. country != language.
		"""
		raise NotImplementedError

	@abstractmethod
	def stay_has_breakfast(self, stay: Stay) -> Optional[bool]:
		"""
		This method is called when we want to know if the stay includes breakfast.
		Notice that the breakfast data is not stored in any of the models?
		How would you deal with this?
		Requirements:
			- Your input is a Stay object.
			- Return True if the stay includes breakfast, otherwise False. Return None if
				you don't know.
		"""
		raise NotImplementedError


class PMS_Mews(PMS):
	def clean_webhook_payload(self, payload: str) -> dict:
		try:
			if not payload:
				raise ValueError('Empty Payload')
			payload_dict = json.loads(payload)
			return payload_dict

		except ValueError as value_error:
			print(f"ValueError: {value_error}")
			return {}
		except json.JSONDecodeError as e:
			print(f"Error decoding JSON payload: {e}")
			return {}


	def handle_webhook(self, webhook_data: dict) -> bool:
		hotel_id = webhook_data.get("HotelId")
		try:
			if not hotel_id:
				raise ValueError("Missing HotelId in the webhook data.")
			hotel = Hotel.objects.get(pms_hotel_id=hotel_id)

			hotel_events = webhook_data.get("Events")
			for event in hotel_events:
				reservation_id = event['Value'].get('ReservationId')
				if not reservation_id:
					raise ValueError("Missing ReservationId in the webhook data.")

				reservation_details = self.clean_webhook_payload(get_reservation_details(reservation_id))
				guest_id = reservation_details.get("GuestId")
				guest_details = self.clean_webhook_payload(get_guest_details(guest_id) if guest_id else "{}")

				with transaction.atomic():
					make_transaction(reservation_details, hotel, reservation_id, guest_details)
			return True

		except APIError as api_error:
			print(f"API Error: {api_error}")
			return False
		except ValueError as value_error:
			print(f"ValueError: {value_error}")
			return False
		except Exception as e:
			print(f"An error occurred: {e}")
			return False

	def update_tomorrows_stays(self) -> bool:
		try:
			# Get the date for tomorrow
			tomorrow = datetime.now() + timedelta(days=1)
			tomorrow_date = tomorrow.date()

			# Get all stays checking in tomorrow using the mock API
			stays_to_update = self.clean_webhook_payload(
				get_reservations_between_dates(tomorrow_date, tomorrow_date + timedelta(days=1))
			)
			for stay_data in stays_to_update:
				# Extract relevant information from the stay_data
				reservation_id = stay_data.get("ReservationId")
				hotel_id = stay_data.get("HotelId")

				if not (reservation_id and hotel_id):
					print(f"Skipping stay with missing information: {stay_data}")
					continue
				reservation_details = self.clean_webhook_payload(get_reservation_details(reservation_id))
				guest_id = reservation_details.get("GuestId")
				hotel = Hotel.objects.get(pms_hotel_id=hotel_id)
				guest_details = self.clean_webhook_payload(get_guest_details(guest_id) if guest_id else "{}")
				with transaction.atomic():
					make_transaction(reservation_details, hotel, reservation_id, guest_details)
			# Return True to indicate successful update of tomorrow's stays
			return True

		except APIError as api_error:
			# Handle API errors (e.g., invalid data, failed API calls)
			print(f"API Error: {api_error}")
			return False
		except Exception as e:
			# Handle other exceptions
			print(f"An error occurred: {e}")
			return False

	def stay_has_breakfast(self, stay: Stay) -> Optional[bool]:
		try:
			reservation_details = self.clean_webhook_payload(get_reservation_details(stay.pms_reservation_id))
			breakfast_included = reservation_details.get("BreakfastIncluded", False)
			return breakfast_included
		except Exception as e:
			# Handle exceptions or return None if unable to determine
			print(f"An error occurred while checking breakfast: {e}")
			return None


def get_pms(name):
	fullname = "PMS_" + name.capitalize()
	# find all class names in this module
	# from https://stackoverflow.com/questions/1796180/
	current_module = sys.modules[__name__]
	clsnames = [x[0] for x in inspect.getmembers(current_module, inspect.isclass)]

	# if we have a PMS class for the given name, return an instance of it
	return getattr(current_module, fullname)() if fullname in clsnames else False


def validate_phone_number(phone_number):
	if not phone_number:
		raise ValueError("Missing Phone in the webhook data.")
	try:
		parsed_phone = phonenumbers.parse(phone_number)
		if not phonenumbers.is_valid_number(parsed_phone):
			raise ValueError("Invalid phone number format")
	except NumberParseException:
		raise ValueError("Invalid phone number format")


def make_transaction(reservation_details, hotel, reservation_id, guest_details):
	# Update or create the Stay model
	stay, _ = Stay.objects.update_or_create(
		pms_reservation_id=reservation_id,
		hotel=hotel,
		defaults={
			'pms_guest_id': reservation_details.get("GuestId"),
			"checkin": reservation_details.get("CheckInDate"),
			"checkout": reservation_details.get("CheckOutDate"),
			"status": reservation_details.get("Status"),
		}
	)
	# Validate phone number using phonenumbers library
	phone_number = guest_details.get("Phone")
	validate_phone_number(phone_number)
	name = guest_details.get("Name")
	# validate name 
	if name is None or not name.strip():
		raise ValueError("Guest name is missing or empty")
	guest, _ = Guest.objects.update_or_create(
		phone=guest_details.get("Phone"),
		defaults={
			"name": name,
			"language": map_country_to_language(guest_details.get("Country")),
		}
	)
	# Assign the guest to the stay
	stay.guest = guest
	stay.save()


def map_country_to_language(country_code):
	# Mapping between countries and languages
	if not country_code:
		return 'No country code'
	country_code = country_code.lower()
	country_language_mapping = {
		"nl": Language.DUTCH,
		"de": Language.GERMAN,
		"en-gb": Language.BRITISH_ENGLISH,
		"es-es": Language.SPANISH_SPAIN,
		"fr": Language.FRENCH,
		"it": Language.ITALIAN,
		"pt-pt": Language.PORTUGUESE_PORTUGAL,
		"sv": Language.SWEDISH,
		"da": Language.DANISH,
	}
	for key, value in country_language_mapping.items():
		if key in country_code:
			return value.label
	return 'Unknown'