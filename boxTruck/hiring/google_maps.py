import requests
from config import settings

def geocode_zip(zip_code):
    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={
                'address': zip_code,
                'key': settings.GOOGLE_MAPS_API_KEY,
            },
            timeout=5,
        )
        data = response.json()
        if data['status'] != 'OK' or not data['results']:
            return None

        result = data['results'][0]
        location = result['geometry']['location']
        components = result['address_components']

        city = state = address = ''
        for component in components:
            types = component['types']
            if 'locality' in types:
                city = component['long_name']
            elif 'administrative_area_level_1' in types:
                state = component['short_name']
            elif 'route' in types:
                address = component['long_name']

        return {
            'lat': location['lat'],
            'lng': location['lng'],
            'city': city,
            'state': state,
            'address': result.get('formatted_address', address),
        }
    except Exception:
        return None
    