from flask import Flask, request, jsonify
import requests
import os # Make sure os is imported
from geopy.distance import geodesic
import logging # For more explicit logging configuration if needed

app = Flask(__name__)

# Configure Flask logging to be more verbose if desired, especially for Vercel
# Vercel captures stdout/stderr, so print statements and default Flask logging work.
# For more control, you could use:
# if not app.debug: # Only set up more detailed logging when not in debug mode (Vercel isn't usually debug)
#     app.logger.setLevel(logging.INFO) # Or logging.DEBUG
#     # You can also add handlers here if you want to send logs elsewhere,
#     # but for Vercel, its own log collection is usually sufficient.

# --- Get API Keys from Environment Variables ---
# These MUST be set in your Vercel Project Environment Variables settings
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
ORS_API_KEY = os.environ.get("ORS_API_KEY") # Set this in Vercel if you use it

if not GOOGLE_MAPS_API_KEY:
    app.logger.critical("CRITICAL STARTUP ERROR: The GOOGLE_MAPS_API_KEY environment variable is not set in Vercel!")
    # This message will appear in your Vercel deployment logs if the key is missing.

# --- Helper function for AI risk scoring ---
def calculate_risk_score(route):
    base_score = 0
    reasons = [] 

    duration_in_traffic_seconds = route.get("legs", [{}])[0].get("duration_in_traffic", {}).get("value", 0)
    if duration_in_traffic_seconds > 0:
        minutes_in_traffic = duration_in_traffic_seconds // 60
        base_score += minutes_in_traffic 
        reasons.append(f"Potential traffic delay: {minutes_in_traffic} minutes")

    hazard_keywords = ["sharp", "roundabout", "merge", "u-turn"]
    hazard_coordinates = []
    
    steps = route.get("legs", [{}])[0].get("steps", [])
    sharp_turns_count = 0
    other_hazardous_maneuvers_count = 0 

    for step in steps:
        instruction = step.get("html_instructions", "").lower()
        is_hazardous_this_step = False
        
        if "sharp" in instruction:
            sharp_turns_count += 1
            is_hazardous_this_step = True
        
        if not ("sharp" in instruction): # Only check other hazards if not already a sharp turn
            if any(keyword in instruction for keyword in hazard_keywords):
                other_hazardous_maneuvers_count +=1
                is_hazardous_this_step = True
        
        if is_hazardous_this_step:
            base_score += 100 
            hazard_coordinates.append(step.get("start_location"))

    if sharp_turns_count > 0:
        reasons.append(f"Route includes {sharp_turns_count} sharp turn(s)")
    if other_hazardous_maneuvers_count > 0:
         reasons.append(f"Route includes {other_hazardous_maneuvers_count} other potentially hazardous maneuver(s) (e.g., roundabouts, merges)")

    ACCIDENT_BLACKSPOTS = [
        {"lat": 11.0180, "lon": 76.9691, "name": "Gandhipuram Signal"},
        {"lat": 10.9946, "lon": 76.9644, "name": "Ukkadam"},
        {"lat": 11.0268, "lon": 77.0357, "name": "Avinashi Road - Hope College"},
        {"lat": 11.0292, "lon": 76.9456, "name": "Mettupalayam Road - Saibaba Colony"},
        {"lat": 11.0028, "lon": 76.9947, "name": "Trichy Road - Ramanathapuram"},
        {"lat": 11.0705, "lon": 76.9981, "name": "Saravanampatti Junction"},
        {"lat": 10.9415, "lon": 76.9695, "name": "Pollachi Road - Eachanari"},
        {"lat": 10.9701, "lon": 76.9410, "name": "Palakkad Road - Kuniyamuthur"}
    ]

    def is_within_radius(coord1, coord2, radius_meters):
        if not all([coord1, coord2, 
                    "lat" in coord1, "lon" in coord1, 
                    "lat" in coord2, "lon" in coord2]):
            return False 
        try:
            return geodesic((coord1["lat"], coord1["lon"]), (coord2["lat"], coord2["lon"])).meters <= radius_meters
        except ValueError:
            app.logger.warning(f"Could not calculate geodesic distance for coords: {coord1}, {coord2}")
            return False

    blackspot_intersections_count = 0
    passed_blackspot_names = set() 

    for step in steps:
        step_location = step.get("start_location", {})
        for blackspot in ACCIDENT_BLACKSPOTS:
            if blackspot["name"] not in passed_blackspot_names:
                if is_within_radius(step_location, blackspot, 250): 
                    base_score += 200 
                    blackspot_intersections_count += 1
                    reasons.append(f"Passes near known accident blackspot: {blackspot['name']}")
                    passed_blackspot_names.add(blackspot["name"]) 
    
    if base_score > 700 and not sharp_turns_count and not blackspot_intersections_count and not other_hazardous_maneuvers_count:
        reasons.append("Route identified as higher risk potentially due to factors like extended traffic duration.")
    elif base_score == 0 and not reasons: 
        reasons.append("Standard route profile based on available data. Always drive safely.")

    return base_score, hazard_coordinates, list(set(reasons))

@app.route('/api/autocomplete', methods=['GET'])
def autocomplete():
    query = request.args.get('query', '')
    if not GOOGLE_MAPS_API_KEY:
        app.logger.warning("/api/autocomplete called but GOOGLE_MAPS_API_KEY is missing from server config.")
    if not query:
        return jsonify([])
    # This is a placeholder. For a real implementation, you'd call Google Places API.
    suggestions = [f"{query} Central", f"{query} Park", f"{query} Station"]
    return jsonify(suggestions)

@app.route('/api/route', methods=['GET'])
def get_route():
    if not GOOGLE_MAPS_API_KEY:
        app.logger.error("Attempted to call /api/route, but GOOGLE_MAPS_API_KEY is not configured on the server.")
        return jsonify({"error": "Server configuration error: API key missing.", "status": "SERVER_CONFIG_ERROR"}), 500
    try:
        required_params = ['originLat', 'originLng', 'destinationLat', 'destinationLng']
        missing_params = [param for param in required_params if param not in request.args]
        if missing_params:
            return jsonify({"error": f"Missing required parameters: {', '.join(missing_params)}", "status": "PARAMS_ERROR"}), 400

        start_lat = float(request.args.get('originLat'))
        start_lon = float(request.args.get('originLng'))
        end_lat = float(request.args.get('destinationLat'))
        end_lon = float(request.args.get('destinationLng'))

    except ValueError: 
        return jsonify({"error": "Coordinate values must be valid numbers.", "status": "VALUE_ERROR"}), 400
    except Exception as e: 
        app.logger.error(f"Parameter parsing error in /api/route: {str(e)}")
        return jsonify({"error": "Invalid request parameters.", "status": "PARAMS_UNKNOWN_ERROR"}), 400

    directions_url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{start_lat},{start_lon}",
        "destination": f"{end_lat},{end_lon}",
        "key": GOOGLE_MAPS_API_KEY,
        "alternatives": "true", 
        "departure_time": "now"
    }

    try:
        api_response = requests.get(directions_url, params=params)
        api_response.raise_for_status() 
        data = api_response.json()

        google_api_status = data.get("status")
        if google_api_status != "OK":
            google_error_message = data.get("error_message", f"Error from Google Directions API: {google_api_status}")
            app.logger.error(f"Google Directions API Error: Status '{google_api_status}' - Message: '{google_error_message}'")
            return jsonify({"error": google_error_message, "status": google_api_status or "GOOGLE_API_ERROR"}), 500

        routes_from_google = data.get("routes", [])
        if not routes_from_google:
            return jsonify({"error": "No routes found between the specified locations.", "status": "NO_ROUTES_FOUND_GOOGLE"}), 404

        processed_routes = []
        for route_detail in routes_from_google:
            raw_score, hazards, reasons_list = calculate_risk_score(route_detail)
            processed_routes.append({
                "polyline": route_detail.get("overview_polyline", {}).get("points"),
                "raw_risk_for_sorting": raw_score, 
                "hazards_coordinates": hazards, 
                "reasons": reasons_list,
                "summary": route_detail.get("summary", "N/A"),
                "duration_text": route_detail.get("legs", [{}])[0].get("duration", {}).get("text", "N/A"),
                "distance_text": route_detail.get("legs", [{}])[0].get("distance", {}).get("text", "N/A"),
            })
        
        if not processed_routes: 
             app.logger.warning("Routes were received from Google but no routes could be processed.")
             return jsonify({"error": "No routes could be processed from Google's response.", "status": "ROUTE_PROCESSING_ERROR"}), 500

        min_raw_risk = min(r['raw_risk_for_sorting'] for r in processed_routes)
        max_raw_risk = max(r['raw_risk_for_sorting'] for r in processed_routes)

        for pr_route in processed_routes:
            if max_raw_risk > min_raw_risk:
                normalized_score = (pr_route['raw_risk_for_sorting'] - min_raw_risk) / (max_raw_risk - min_raw_risk)
            elif len(processed_routes) >= 1: 
                 normalized_score = 0.0 
            else: 
                 normalized_score = 0.0 
            
            pr_route['risk_score'] = 0.05 + (normalized_score * 0.95)
            pr_route['risk_score'] = max(0.05, min(1.0, pr_route['risk_score']))
            
            del pr_route['raw_risk_for_sorting']

        return jsonify({
            "status": "OK", 
            "routes": processed_routes
        })

    except requests.exceptions.HTTPError as e:
        app.logger.error(f"HTTP error calling Google API: {str(e)} - Response: {e.response.text if e.response else 'No response body'}")
        try:
            error_data = e.response.json() if e.response else {}
            error_message = error_data.get("error_message", error_data.get("error", str(e)))
            error_status = error_data.get("status", "GOOGLE_HTTP_ERROR")
        except ValueError: 
            error_message = e.response.text if e.response else str(e)
            error_status = "GOOGLE_HTTP_ERROR_NON_JSON"
        return jsonify({"error": error_message, "status": error_status}), e.response.status_code if e.response else 500
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Network error calling Google API: {str(e)}")
        return jsonify({"error": f"Network error when fetching directions: {str(e)}", "status": "NETWORK_ERROR_GOOGLE_API"}), 503
    except Exception as e:
        app.logger.error(f"Unexpected error in /api/route: {type(e).__name__} - {str(e)}")
        import traceback
        app.logger.error(traceback.format_exc()) 
        return jsonify({"error": f"An unexpected server error occurred.", "status": "UNKNOWN_SERVER_ERROR"}), 500

@app.route('/', methods=['GET'])
def home():
    if not GOOGLE_MAPS_API_KEY:
        # This will be logged by Vercel when the app starts if the key is missing.
        # This route just confirms the app is running but might be unhealthy if key is missing.
        return jsonify({'status': 'unhealthy', 'message': 'Helios Backend is live, but GOOGLE_MAPS_API_KEY is MISSING from server configuration!'})
    return jsonify({'status': 'healthy', 'message': 'Helios Google-Powered Backend is live!'})
