// API Helper with rate limiting and exponential backoff
let activeRequests = 0;
const MAX_CONCURRENT_REQUESTS = 2; // Only 2 requests at a time
const requestQueue = [];
let retryCount = {};

// Process the queue when requests complete
function processQueue() {
  while (requestQueue.length > 0 && activeRequests < MAX_CONCURRENT_REQUESTS) {
    const nextRequest = requestQueue.shift();
    executeRequest(nextRequest.url, nextRequest.resolve, nextRequest.reject);
  }
}

// Execute a request with proper resource management
function executeRequest(url, resolve, reject) {
  activeRequests++;
  
  // Add a random delay between 100-500ms to prevent exact simultaneous requests
  setTimeout(() => {
    fetch(url)
      .then(response => {
        activeRequests--;
        // Reset retry count on success
        retryCount[url] = 0;
        resolve(response.json());
        // Process next request
        processQueue();
      })
      .catch(error => {
        activeRequests--;
        
        // Initialize retry count if not exists
        if (!retryCount[url]) {
          retryCount[url] = 0;
        }
        
        if (retryCount[url] < 3) { // Max 3 retries
          console.log(`Retrying ${url} (attempt ${retryCount[url] + 1})`);
          retryCount[url]++;
          
          // Exponential backoff
          const delay = Math.pow(2, retryCount[url]) * 1000;
          setTimeout(() => {
            // Re-queue with higher priority
            requestQueue.unshift({ url, resolve, reject });
            processQueue();
          }, delay);
        } else {
          console.error(`Max retries reached for ${url}`);
          reject(error);
          // Process next request
          processQueue();
        }
      });
  }, Math.random() * 400 + 100);
}

// Rate-limited fetch function
export function fetchWithLimit(url) {
  return new Promise((resolve, reject) => {
    if (activeRequests < MAX_CONCURRENT_REQUESTS) {
      executeRequest(url, resolve, reject);
    } else {
      // Queue the request
      requestQueue.push({ url, resolve, reject });
    }
  });
}
