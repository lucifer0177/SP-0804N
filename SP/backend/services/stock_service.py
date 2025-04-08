import pandas as pd
import numpy as np
import os
import time
import threading
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv
import logging
import random
import backoff
from functools import wraps
import queue
from collections import deque

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('StockService')

# Load environment variables
load_dotenv()

# Rate limiter class for Yahoo Finance API
class RateLimiter:
    """Rate limiter for API calls"""
    def __init__(self, max_calls=30, period=60):
        self.timestamps = deque()
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
    
    def wait_if_needed(self):
        """Wait if rate limit would be exceeded"""
        now = time.time()
        with self.lock:
            # Remove expired timestamps
            while self.timestamps and now - self.timestamps[0] > self.period:
                self.timestamps.popleft()
            
            # If at rate limit, wait until oldest timestamp expires
            if len(self.timestamps) >= self.max_calls:
                sleep_time = self.timestamps[0] + self.period - now + random.uniform(0.1, 1.0)
                time.sleep(max(0, sleep_time))
                now = time.time()
            
            # Add current timestamp
            self.timestamps.append(now)

# Create a global rate limiter for Yahoo Finance
YAHOO_RATE_LIMITER = RateLimiter(max_calls=25, period=60)  # 25 calls per minute (conservative)

# Decorate functions to apply rate limiting
def rate_limit(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        YAHOO_RATE_LIMITER.wait_if_needed()
        return func(*args, **kwargs)
    return wrapper

class StockService:
    """Service for interacting with stock market data sources using yfinance with improved rate limiting"""
    
    # Cache TTLs in seconds
    REALTIME_TTL = 30      # 30 seconds for real-time data
    HISTORICAL_TTL = 3600  # 1 hour for historical data
    SEARCH_TTL = 3600      # 1 hour for search results
    MARKET_TTL = 60        # 60 seconds for market data
    
    # Create a thread pool for batch processing
    MAX_WORKERS = 2  # Limit concurrent workers
    
    def __init__(self):
        # Initialize cache
        self.cache = {
            'realtime': {},
            'historical': {},
            'search': {},
            'market': {}
        }
        
        # Thread lock for cache operations
        self.lock = threading.Lock()
        
        # Work queue for batch operations
        self.work_queue = queue.Queue()
        
        # Worker threads
        self.workers = []
        for _ in range(self.MAX_WORKERS):
            worker = threading.Thread(target=self._worker_thread, daemon=True)
            worker.start()
            self.workers.append(worker)
        
        # Start cache cleanup thread
        cleanup_thread = threading.Thread(target=self._cleanup_task, daemon=True)
        cleanup_thread.start()
        
        # Popular stock symbols for fallback
        self.popular_stocks = [
            {"symbol": "AAPL", "name": "Apple Inc."},
            {"symbol": "MSFT", "name": "Microsoft Corporation"},
            {"symbol": "GOOGL", "name": "Alphabet Inc."},
            {"symbol": "AMZN", "name": "Amazon.com Inc."},
            {"symbol": "META", "name": "Meta Platforms Inc."},
            {"symbol": "TSLA", "name": "Tesla Inc."},
            {"symbol": "NVDA", "name": "NVIDIA Corporation"},
            {"symbol": "JPM", "name": "JPMorgan Chase & Co."},
            {"symbol": "V", "name": "Visa Inc."},
            {"symbol": "JNJ", "name": "Johnson & Johnson"}
        ]
        
        logger.info("StockService initialized with improved rate limiting")
    
    def _worker_thread(self):
        """Worker thread for processing tasks from the queue"""
        while True:
            try:
                task, args, kwargs, result_queue = self.work_queue.get()
                try:
                    result = task(*args, **kwargs)
                    result_queue.put(("success", result))
                except Exception as e:
                    logger.error(f"Error in worker thread: {e}")
                    result_queue.put(("error", str(e)))
                finally:
                    self.work_queue.task_done()
            except Exception as e:
                logger.error(f"Worker thread error: {e}")
    
    def _cleanup_task(self):
        """Background task to clean up expired cache entries"""
        while True:
            try:
                time.sleep(300)  # Run every 5 minutes
                self._cleanup_cache()
            except Exception as e:
                logger.error(f"Error in cleanup task: {e}")
    
    def _cleanup_cache(self):
        """Remove expired entries from cache"""
        now = time.time()
        expired_counts = {cache_type: 0 for cache_type in self.cache}
        
        with self.lock:
            for cache_type, ttl_value in [
                ('realtime', self.REALTIME_TTL),
                ('historical', self.HISTORICAL_TTL),
                ('search', self.SEARCH_TTL),
                ('market', self.MARKET_TTL)
            ]:
                expired_keys = [
                    key for key, value in self.cache[cache_type].items() 
                    if now - value['timestamp'] > ttl_value
                ]
                
                for key in expired_keys:
                    del self.cache[cache_type][key]
                    expired_counts[cache_type] += 1
        
        for cache_type, count in expired_counts.items():
            if count > 0:
                logger.debug(f"Cleaned up {count} expired entries from {cache_type} cache")
    
    def _safe_fetch_ticker(self, symbol):
        """Safely fetch a ticker with rate limiting and error handling"""
        try:
            # Apply rate limiting
            YAHOO_RATE_LIMITER.wait_if_needed()
            
            # Get ticker
            ticker = yf.Ticker(symbol)
            
            # Test if ticker info works (will raise exception if not)
            _ = ticker.info
            
            return ticker
        except Exception as e:
            logger.warning(f"Error fetching ticker for {symbol}: {e}")
            return None
    
    def _safe_get_info(self, ticker, symbol):
        """Safely get ticker info with retries"""
        if ticker is None:
            return {}
            
        # Try up to 3 times with exponential backoff
        for attempt in range(3):
            try:
                # Apply rate limiting on each attempt
                YAHOO_RATE_LIMITER.wait_if_needed()
                
                info = ticker.info
                if info:
                    return info
                else:
                    logger.warning(f"Empty info for {symbol}, attempt {attempt+1}/3")
            except Exception as e:
                logger.warning(f"Error getting info for {symbol}, attempt {attempt+1}/3: {e}")
                
            # Only sleep if we'll try again
            if attempt < 2:
                sleep_time = (2 ** attempt) + random.random()
                time.sleep(sleep_time)
        
        return {}  # Return empty dict after all failures
    
    def _get_cached_or_execute(self, cache_type, cache_key, fetch_func, ttl=None):
        """Get data from cache or execute fetch function"""
        if ttl is None:
            ttl = getattr(self, f"{cache_type.upper()}_TTL")
            
        now = time.time()
        
        # Check cache first
        with self.lock:
            if cache_key in self.cache[cache_type] and now - self.cache[cache_type][cache_key]['timestamp'] < ttl:
                logger.debug(f"Cache hit for {cache_key}")
                return self.cache[cache_type][cache_key]['data']
        
        # Execute fetch function
        try:
            result = fetch_func()
            
            # Cache result
            with self.lock:
                self.cache[cache_type][cache_key] = {
                    'data': result,
                    'timestamp': now
                }
            
            return result
        except Exception as e:
            logger.error(f"Error fetching data for {cache_key}: {e}")
            
            # Try cached data even if expired
            with self.lock:
                if cache_key in self.cache[cache_type]:
                    logger.info(f"Using expired cached data for {cache_key} after fetch failure")
                    return self.cache[cache_type][cache_key]['data']
            
            # Propagate the exception if we have no fallback
            raise
    
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def search_stocks(self, query, limit=10):
        """Search for stocks based on a query string with rate limiting"""
        if not query:
            # Return popular stocks if no query
            return self.popular_stocks[:limit]
        
        cache_key = f"search_{query}_{limit}"
        
        def fetch_data():
            results = []
            
            # First try direct symbol match
            if len(query) <= 5:  # Likely a symbol
                try:
                    ticker = self._safe_fetch_ticker(query.upper())
                    if ticker:
                        info = self._safe_get_info(ticker, query)
                        if info:
                            results.append({
                                "symbol": query.upper(),
                                "name": info.get('shortName', info.get('longName', f"{query.upper()} Inc."))
                            })
                except Exception as e:
                    logger.debug(f"Error in direct symbol search for {query}: {e}")
            
            # If no direct match or name search, filter popular stocks
            if not results:
                # Filter popular stocks by query
                query_lower = query.lower()
                results = [
                    stock for stock in self.popular_stocks 
                    if query_lower in stock['symbol'].lower() or query_lower in stock['name'].lower()
                ]
            
            return results[:limit]
        
        return self._get_cached_or_execute('search', cache_key, fetch_data)
    
    def get_batch_stock_details(self, symbols):
        """Get details for multiple stocks efficiently with rate limiting"""
        if not symbols:
            return {}
            
        results = {}
        result_queue = queue.Queue()
        
        # First check cache for all symbols
        with self.lock:
            now = time.time()
            for symbol in symbols:
                cache_key = f"details_{symbol}"
                if cache_key in self.cache['realtime'] and now - self.cache['realtime'][cache_key]['timestamp'] < self.REALTIME_TTL:
                    results[symbol] = self.cache['realtime'][cache_key]['data']
        
        # Process uncached symbols with limited concurrency
        symbols_to_fetch = [s for s in symbols if s not in results]
        
        if symbols_to_fetch:
            # Process symbols in smaller batches to prevent rate limit issues
            batch_size = 3  # Process in small batches
            for i in range(0, len(symbols_to_fetch), batch_size):
                batch = symbols_to_fetch[i:i+batch_size]
                
                # Process each symbol in the batch
                for symbol in batch:
                    # Slight delay between processing symbols in a batch
                    time.sleep(random.uniform(0.5, 1.0))
                    try:
                        results[symbol] = self.get_stock_details(symbol)
                    except Exception as e:
                        logger.error(f"Error in batch processing for {symbol}: {e}")
                        results[symbol] = self._get_mock_stock_details(symbol)
                
                # Add delay between batches
                if i + batch_size < len(symbols_to_fetch):
                    time.sleep(random.uniform(1.0, 2.0))
        
        return results
    
    @rate_limit
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_stock_details(self, symbol):
        """Get detailed stock information with enhanced rate limiting and error handling"""
        symbol = symbol.upper()
        cache_key = f"details_{symbol}"
        
        def fetch_data():
            # Add jitter to avoid synchronized API calls
            time.sleep(random.uniform(0.1, 0.3))
            
            # Get ticker object
            ticker = self._safe_fetch_ticker(symbol)
            if not ticker:
                raise ValueError(f"Failed to obtain ticker for {symbol}")
            
            # Get detailed info
            info = self._safe_get_info(ticker, symbol)
            if not info:
                raise ValueError(f"Failed to get info for {symbol}")
            
            # Get historical data for recent prices (with rate limiting)
            YAHOO_RATE_LIMITER.wait_if_needed()
            hist = ticker.history(period="2d")
            
            # Calculate changes
            if len(hist) >= 2:
                current_price = hist['Close'].iloc[-1]
                prev_close = hist['Close'].iloc[-2]
                change = current_price - prev_close
                percent_change = (change / prev_close * 100) if prev_close > 0 else 0
            else:
                current_price = info.get('currentPrice', info.get('regularMarketPrice', 0))
                prev_close = info.get('previousClose', current_price)
                change = current_price - prev_close
                percent_change = (change / prev_close * 100) if prev_close > 0 else 0
            
            # Get analyst recommendations with safe handling
            analyst = {"buy": 0, "hold": 0, "sell": 0}
            try:
                if random.random() < 0.3:  # Only fetch recommendations 30% of the time to reduce API load
                    YAHOO_RATE_LIMITER.wait_if_needed()
                    recommendations = ticker.recommendations
                    
                    if recommendations is not None and not recommendations.empty:
                        recent_recs = recommendations.tail(10)  # Last 10 recommendations
                        
                        if 'To Grade' in recent_recs.columns:
                            for grade in recent_recs['To Grade']:
                                if pd.isna(grade):  # Skip NaN values
                                    continue
                                    
                                grade_lower = str(grade).lower()
                                if 'buy' in grade_lower or 'outperform' in grade_lower or 'overweight' in grade_lower:
                                    analyst["buy"] += 1
                                elif 'hold' in grade_lower or 'neutral' in grade_lower or 'market perform' in grade_lower:
                                    analyst["hold"] += 1
                                elif 'sell' in grade_lower or 'underperform' in grade_lower or 'underweight' in grade_lower:
                                    analyst["sell"] += 1
            except Exception as e:
                logger.debug(f"Error processing recommendations for {symbol}: {e}")
            
            # Process additional data safely with fallbacks
            market_cap = info.get('marketCap', 0) / 1e12  # in trillions
            pe = info.get('trailingPE', info.get('forwardPE', 0))
            eps = info.get('trailingEps', 0)
            dividend = info.get('dividendYield', 0) * 100 if info.get('dividendYield') else 0  # as percentage
            
            return {
                "symbol": symbol,
                "name": info.get('shortName', info.get('longName', f"{symbol} Corp")),
                "price": current_price,
                "change": change,
                "percentChange": percent_change,
                "marketCap": round(market_cap, 2),
                "volume": round(info.get('volume', info.get('regularMarketVolume', 0)) / 1e6, 1),  # Volume in millions
                "avgVolume": round(info.get('averageVolume', info.get('averageDailyVolume10Day', 0)) / 1e6, 1),
                "pe": round(pe, 1) if pe else 0,
                "eps": round(eps, 2) if eps else 0,
                "dividend": round(dividend, 2),
                "high52w": info.get('fiftyTwoWeekHigh', 0),
                "low52w": info.get('fiftyTwoWeekLow', 0),
                "open": info.get('open', info.get('regularMarketOpen', 0)),
                "previousClose": prev_close,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dataSource": "yfinance",
                "analyst": analyst
            }
        
        try:
            return self._get_cached_or_execute('realtime', cache_key, fetch_data)
        except Exception as e:
            logger.error(f"Failed to get stock details for {symbol}: {e}")
            return self._get_mock_stock_details(symbol)
    
    def _get_mock_stock_details(self, symbol):
        """Generate mock stock details when API fails"""
        logger.info(f"Generating mock data for {symbol}")
        
        # Use symbol to generate consistent random data
        random.seed(hash(symbol) % 10000)
        
        base_price = 50 + (hash(symbol) % 100)
        
        return {
            "symbol": symbol,
            "name": f"{symbol} Corporation",
            "price": round(base_price + random.uniform(-5, 5), 2),
            "change": round(random.uniform(-2, 2), 2),
            "percentChange": round(random.uniform(-2, 2), 2),
            "marketCap": round(random.uniform(10, 500) / 100, 2),  # In billions
            "volume": round(random.uniform(1, 10), 1),  # In millions
            "avgVolume": round(random.uniform(1, 10), 1),  # In millions
            "pe": round(random.uniform(10, 30), 1),
            "eps": round(random.uniform(1, 10), 2),
            "dividend": round(random.uniform(0, 3), 2),
            "high52w": round(base_price * 1.2, 2),
            "low52w": round(base_price * 0.8, 2),
            "open": round(base_price - random.uniform(-2, 2), 2),
            "previousClose": round(base_price - random.uniform(-1, 1), 2),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dataSource": "mock",
            "analyst": {"buy": random.randint(0, 10), "hold": random.randint(0, 5), "sell": random.randint(0, 3)}
        }
    
    @rate_limit
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_historical_data(self, symbol, timeframe='1m'):
        """Get historical price data with improved rate limiting"""
        symbol = symbol.upper()
        cache_key = f"historical_{symbol}_{timeframe}"
        
        def fetch_data():
            # Map timeframe to yfinance parameters
            if timeframe == '1d':
                period = "1d"
                interval = "5m"
                date_format = '%H:%M'
            elif timeframe == '1w':
                period = "1wk"
                interval = "1h"
                date_format = '%a'
            elif timeframe == '1m':
                period = "1mo"
                interval = "1d"
                date_format = '%d'
            elif timeframe == '3m':
                period = "3mo"
                interval = "1d"
                date_format = '%b %d'
            elif timeframe == '1y':
                period = "1y"
                interval = "1wk"
                date_format = '%b'
            else:  # 'all' or default
                period = "max"
                interval = "1mo"
                date_format = '%Y'
            
            # Get ticker data with rate limiting
            ticker = self._safe_fetch_ticker(symbol)
            if not ticker:
                raise ValueError(f"Failed to obtain ticker for {symbol}")
            
            # Get historical data with rate limiting
            YAHOO_RATE_LIMITER.wait_if_needed()
            hist = ticker.history(period=period, interval=interval)
            
            if hist.empty:
                raise ValueError(f"No historical data returned for {symbol}")
            
            # Convert to lists for JSON
            timestamps = hist.index.tolist()
            prices = hist['Close'].tolist()
            
            # Format dates
            if isinstance(timestamps[0], pd.Timestamp):
                dates = [ts.strftime('%Y-%m-%d') for ts in timestamps]
                labels = [ts.strftime(date_format) for ts in timestamps]
            else:
                # If timestamps are already strings
                dates = timestamps
                labels = timestamps
            
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "labels": labels,
                "data": [round(price, 2) if not pd.isna(price) else None for price in prices],
                "timestamps": dates,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updated_by": "lucifer0177",
                "dataSource": "yfinance"
            }
        
        try:
            return self._get_cached_or_execute('historical', cache_key, fetch_data)
        except Exception as e:
            logger.error(f"Failed to get historical data for {symbol}: {e}")
            return self._get_mock_historical_data(symbol, timeframe)
    
    def _get_mock_historical_data(self, symbol, timeframe='1m'):
        """Generate mock historical data when API fails"""
        logger.info(f"Generating mock historical data for {symbol}")
        
        # Map timeframe to parameters
        if timeframe == '1d':
            days = 1
            points = 24
            date_format = '%H:%M'
        elif timeframe == '1w':
            days = 7
            points = 7
            date_format = '%a'
        elif timeframe == '1m':
            days = 30
            points = 30
            date_format = '%d'
        elif timeframe == '3m':
            days = 90
            points = 12
            date_format = '%b %d'
        elif timeframe == '1y':
            days = 365
            points = 52
            date_format = '%b'
        else:  # 'all'
            days = 1825  # 5 years
            points = 60
            date_format = '%Y'
        
        # Create timestamps
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        timestamps = pd.date_range(start=start_date, end=end_date, periods=points)
        
        # Create consistent mock price data based on symbol
        random.seed(hash(symbol) % 10000)  # Use symbol for consistent randomness
        
        start_price = 50 + (hash(symbol) % 200)  # Base price on symbol hash
        volatility = 0.02 + (hash(symbol) % 10) / 100.0  # Symbol-specific volatility
        
        returns = np.random.normal(0, volatility, points)
        prices = [start_price]
        
        for ret in returns[1:]:
            # Add some seasonality/trend based on symbol hash
            trend = 0.001 * (hash(symbol) % 5 - 2)  # Small upward or downward trend
            prices.append(prices[-1] * (1 + ret + trend))
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "labels": [ts.strftime(date_format) for ts in timestamps],
            "data": [round(price, 2) for price in prices],
            "timestamps": [ts.strftime("%Y-%m-%d") for ts in timestamps],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_by": "mock_data",
            "dataSource": "mock"
        }
    
    @rate_limit
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_market_summary(self):
        """Get market summary data with improved rate limiting"""
        cache_key = "market_summary"
        
        def fetch_data():
            # Major indices to fetch (limit number to reduce API load)
            indices_symbols = {
                "S&P 500": "^GSPC", 
                "Dow Jones": "^DJI", 
                "Nasdaq": "^IXIC"
            }
            
            # Limit sector ETFs to reduce API load
            sector_symbols = {
                "Technology": "XLK",
                "Healthcare": "XLV",
                "Financials": "XLF",
                "Energy": "XLE",
                "Consumer": "XLY"
            }
            
            indices_data = []
            for name, symbol in indices_symbols.items():
                try:
                    # Apply rate limiting
                    YAHOO_RATE_LIMITER.wait_if_needed()
                    
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period="2d")
                    
                    if len(hist) >= 2:
                        current_price = hist['Close'].iloc[-1]
                        prev_close = hist['Close'].iloc[-2]
                        change = current_price - prev_close
                        percent_change = (change / prev_close * 100) if prev_close > 0 else 0
                        
                        indices_data.append({
                            "name": name,
                            "value": round(current_price, 2),
                            "change": round(change, 2),
                            "percentChange": round(percent_change, 2)
                        })
                except Exception as e:
                    logger.error(f"Error getting data for index {name}: {e}")
                
                # Add delay between index requests
                time.sleep(random.uniform(0.5, 1.0))
            
            # Only get sector data if we have at least one index
            sector_performance = []
            if indices_data:
                for name, symbol in sector_symbols.items():
                    try:
                        # Apply rate limiting
                        YAHOO_RATE_LIMITER.wait_if_needed()
                        
                        ticker = yf.Ticker(symbol)
                        hist = ticker.history(period="2d")
                        
                        if len(hist) >= 2:
                            current_price = hist['Close'].iloc[-1]
                            prev_close = hist['Close'].iloc[-2]
                            percent_change = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0
                            
                            sector_performance.append({
                                "name": name,
                                "percentChange": round(percent_change, 2)
                            })
                    except Exception as e:
                        logger.error(f"Error getting data for sector {name}: {e}")
                    
                    # Add delay between sector requests
                    time.sleep(random.uniform(0.5, 1.0))
            
            # Determine market status
            market_status = self._get_market_status()
            
            return {
                "indices": indices_data,
                "sectorPerformance": sector_performance,
                "marketStatus": market_status,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updatedBy": "lucifer0177",
                "dataSource": "yfinance"
            }
        
        try:
            return self._get_cached_or_execute('market', cache_key, fetch_data)
        except Exception as e:
            logger.error(f"Failed to get market summary: {e}")
            return self._get_mock_market_summary()
    
    def _get_market_status(self):
        """Determine if the market is currently open"""
        now = datetime.now()
        weekday = now.weekday()  # 0-6 (Mon-Sun)
        
        # US market hours: 9:30 AM to 4:00 PM Eastern Time
        if weekday < 5:  # Monday to Friday
            # Convert current time to Eastern Time (rough approximation)
            eastern_hour = (now.hour - 4) % 24  # UTC-4 for Eastern Time
            
            if 9 <= eastern_hour < 16 or (eastern_hour == 9 and now.minute >= 30):
                return "open"
            
        return "closed"
    
    def _get_mock_market_summary(self):
        """Return mock market summary data"""
        return {
            "indices": [
                {"name": "S&P 500", "value": 5280.14, "change": 42.32, "percentChange": 0.81},
                {"name": "Dow Jones", "value": 38905.66, "change": 156.87, "percentChange": 0.40},
                {"name": "Nasdaq", "value": 16742.39, "change": -23.87, "percentChange": -0.14}
            ],
            "sectorPerformance": [
                {"name": "Technology", "percentChange": 1.53},
                {"name": "Healthcare", "percentChange": 0.87},
                {"name": "Financials", "percentChange": -0.42},
                {"name": "Energy", "percentChange": -1.32},
                {"name": "Consumer", "percentChange": 0.35}
            ],
            "marketStatus": "closed",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updatedBy": "mock_data",
            "dataSource": "mock"
        }
    
    @rate_limit
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    def get_market_movers(self, limit=5):
        """Get market movers using yfinance with improved rate limiting"""
        # Reduce limit if it's too high to avoid excessive API calls
        limit = min(limit, 5)
        cache_key = f"movers_{limit}"
        
        def fetch_data():
            # Reduced list of stocks to check
            major_stocks = [
                "AAPL", "MSFT", "GOOGL", "AMZN", "META",
                "TSLA", "NVDA", "JPM", "V", "JNJ"
            ]
            
            # Shuffle to get different results each time
            random.shuffle(major_stocks)
            
            stock_data = []
            for symbol in major_stocks:
                try:
                    ticker = self._safe_fetch_ticker(symbol)
                    if not ticker:
                        continue
                    
                    # Apply rate limiting
                    YAHOO_RATE_LIMITER.wait_if_needed()
                    
                    hist = ticker.history(period="2d")
                    info = self._safe_get_info(ticker, symbol)
                    
                    if len(hist) >= 2 and info:
                        current_price = hist['Close'].iloc[-1]
                        prev_close = hist['Close'].iloc[-2]
                        change = current_price - prev_close
                        percent_change = (change / prev_close * 100) if prev_close > 0 else 0
                        
                        stock_data.append({
                            "symbol": symbol,
                            "name": info.get('shortName', f"{symbol} Inc."),
                            "price": round(current_price, 2),
                            "change": round(change, 2),
                            "percentChange": round(percent_change, 2)
                        })
                except Exception as e:
                    logger.error(f"Error getting data for {symbol}: {e}")
                
                # Add delay between requests
                time.sleep(random.uniform(0.5, 1.0))
            
            # Sort by percent change
            gainers = sorted(stock_data, key=lambda x: x['percentChange'], reverse=True)[:limit]
            losers = sorted(stock_data, key=lambda x: x['percentChange'])[:limit]
            
            return {
                "gainers": gainers,
                "losers": losers,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updatedBy": "lucifer0177",
                "dataSource": "yfinance"
            }
        
        try:
            return self._get_cached_or_execute('market', cache_key, fetch_data)
        except Exception as e:
            logger.error(f"Failed to get market movers: {e}")
            return self._get_mock_market_movers()
    
    def _get_mock_market_movers(self):
        """Generate mock market movers data"""
        return {
            "gainers": [
                {"symbol": "NVDA", "name": "NVIDIA Corporation", "price": 950.37, "change": 48.78, "percentChange": 5.42},
                {"symbol": "TSLA", "name": "Tesla Inc", "price": 178.22, "change": 6.50, "percentChange": 3.78},
                {"symbol": "AAPL", "name": "Apple Inc", "price": 243.56, "change": 3.21, "percentChange": 1.34},
                {"symbol": "AMZN", "name": "Amazon.com Inc", "price": 181.75, "change": 1.92, "percentChange": 1.07},
                {"symbol": "GOOGL", "name": "Alphabet Inc", "price": 187.63, "change": 1.75, "percentChange": 0.94}
            ],
            "losers": [
                {"symbol": "META", "name": "Meta Platforms Inc", "price": 475.12, "change": -12.10, "percentChange": -2.48},
                {"symbol": "JPM", "name": "JPMorgan Chase & Co", "price": 178.92, "change": -3.38, "percentChange": -1.85},
                {"symbol": "MSFT", "name": "Microsoft Corporation", "price": 420.87, "change": -2.53, "percentChange": -0.60},
                {"symbol": "JNJ", "name": "Johnson & Johnson", "price": 147.62, "change": -0.75, "percentChange": -0.51},
                {"symbol": "V", "name": "Visa Inc", "price": 298.45, "change": -1.02, "percentChange": -0.34}
            ],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updatedBy": "mock_data",
            "dataSource": "mock"
        }
    
    @rate_limit
    def get_most_watched(self, limit=5):
        """Get most watched/popular stocks with improved rate limiting"""
        # Limit to 5 at most
        limit = min(limit, 5)
        cache_key = f"watched_{limit}"
        
        def fetch_data():
            # In a real app, this would be based on user activity
            # For now, return data for popular tech stocks
            popular_symbols = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA"][:limit]
            
            watched = []
            for symbol in popular_symbols:
                try:
                    # Use batch stock details to leverage caching
                    stock = self.get_stock_details(symbol)
                    if stock:
                        watched.append(stock)
                except Exception as e:
                    logger.error(f"Error getting watched stock details for {symbol}: {e}")
                
                # Add delay between requests
                time.sleep(random.uniform(0.3, 0.6))
            
            return {
                "stocks": watched,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updatedBy": "lucifer0177",
                "dataSource": watched[0]['dataSource'] if watched else "unknown"
            }
        
        try:
            return self._get_cached_or_execute('market', cache_key, fetch_data)
        except Exception as e:
            logger.error(f"Failed to get most watched: {e}")
            return self._get_mock_most_watched()
    
    def _get_mock_most_watched(self):
        """Generate mock most watched data"""
        return {
            "stocks": [
                {"symbol": "AAPL", "name": "Apple Inc.", "price": 243.56, "change": 3.21, "percentChange": 1.34},
                {"symbol": "MSFT", "name": "Microsoft Corporation", "price": 420.87, "change": -2.53, "percentChange": -0.60},
                {"symbol": "GOOGL", "name": "Alphabet Inc.", "price": 187.63, "change": 1.75, "percentChange": 0.94},
                {"symbol": "AMZN", "name": "Amazon.com Inc.", "price": 181.75, "change": 1.92, "percentChange": 1.07},
                {"symbol": "TSLA", "name": "Tesla Inc.", "price": 178.22, "change": 6.50, "percentChange": 3.78}
            ],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updatedBy": "mock_data",
            "dataSource": "mock"
        }