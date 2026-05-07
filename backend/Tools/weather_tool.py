from langchain.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a given city.
    
    Args:
        city: The name of the city to get weather for.
    """
    
    weather_data = {
        "london": "Cloudy, 14°C with light rain",
        "new york": "Sunny, 22°C with clear skies",
        "tokyo": "Partly cloudy, 18°C",
        "colombo": "Hot and humid, 31°C with sunshine",
    }
    result = weather_data.get(city.lower(), f"Weather data not available for {city}")
    return result