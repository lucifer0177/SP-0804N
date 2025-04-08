import threading
import time
import logging

logger = logging.getLogger("CacheCleanup")

class CacheCleanupService:
    """Service to periodically clean up stale cache entries"""
    
    def __init__(self, stock_service, interval=300):
        """Initialize with service and cleanup interval (seconds)"""
        self.stock_service = stock_service
        self.interval = interval
        self.running = False
        self.thread = None
        
    def start(self):
        """Start the cleanup thread"""
        if self.running:
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.thread.start()
        logger.info("Cache cleanup service started")
        
    def stop(self):
        """Stop the cleanup thread"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
        logger.info("Cache cleanup service stopped")
        
    def _cleanup_loop(self):
        """Main cleanup loop"""
        while self.running:
            try:
                self._perform_cleanup()
            except Exception as e:
                logger.error(f"Error during cache cleanup: {e}")
            
            # Sleep for the specified interval
            time.sleep(self.interval)
            
    def _perform_cleanup(self):
        """Clean up stale cache entries"""
        now = time.time()
        removed_count = 0
        
        with self.stock_service.lock:
            # Clean realtime cache
            stale_keys = []
            for key, item in self.stock_service.cache['realtime'].items():
                if now - item['timestamp'] > self.stock_service.REALTIME_TTL + 60:  # Add buffer
                    stale_keys.append(key)
                    
            for key in stale_keys:
                del self.stock_service.cache['realtime'][key]
                removed_count += 1
            
            # Clean historical cache
            stale_keys = []
            for key, item in self.stock_service.cache['historical'].items():
                if now - item['timestamp'] > self.stock_service.HISTORICAL_TTL + 60:  # Add buffer
                    stale_keys.append(key)
                    
            for key in stale_keys:
                del self.stock_service.cache['historical'][key]
                removed_count += 1
            
        logger.info(f"Cleaned up {removed_count} stale cache entries")