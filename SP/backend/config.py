import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Alpha Vantage API key
ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY', 'demo')

# Finnhub API key (optional alternative)
FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY', '')

# Polygon API key (optional alternative)
POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY', '')

# Application settings
DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
PORT = int(os.environ.get('PORT', 5000))

# API Rate Limiting Configuration
RATE_LIMITS = {
    'default': '100 per minute',
    'stocks': '30 per minute',
    'historical': '20 per minute',
    'predict': '10 per minute'
}

# Database Connection Pool Settings
DB_POOL_SIZE = 5
DB_MAX_OVERFLOW = 3
DB_POOL_RECYCLE = 3600  # Recycle connections after 1 hour
DB_POOL_TIMEOUT = 30    # Wait 30 seconds for connection

# Request Timeout Settings
REQUEST_TIMEOUT = 30    # Seconds

# Resource Management
MAX_WORKERS = 4         # Maximum worker threads
MAX_MEMORY_USAGE = 0.8  # 80% of available memory
