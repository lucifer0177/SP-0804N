import React, { createContext, useState, useContext, useEffect, useRef } from 'react';
import { fetchWithLimit } from '../utils/api-helper';

const RealTimeDataContext = createContext();

export const useRealTimeData = () => useContext(RealTimeDataContext);

export const RealTimeDataProvider = ({ children }) => {
  const [realTimeStocks, setRealTimeStocks] = useState({});
  const [lastUpdated, setLastUpdated] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const [watchlist, setWatchlist] = useState([]);
  const refreshTimerRef = useRef(null);
  const currentDate = new Date("2025-04-03 07:12:05");
  const currentUser = "lucifer0177continue";

  // Function to fetch latest stock data
  const fetchLatestData = async (symbols) => {
    if (!symbols || symbols.length === 0) return;
    
    setIsLoading(true);
    
    try {
      const stockPromises = symbols.map(symbol => 
        fetchWithLimit(`/api/stocks/${symbol.toLowerCase()}`)
          .then(data => ({ symbol, data: data.data }))
          .catch(err => {
            console.error(`Error fetching data for ${symbol}:`, err);
            return { symbol, error: err.message };
          })
      );
      
      const results = await Promise.all(stockPromises);
      const newStockData = { ...realTimeStocks };
      
      results.forEach(result => {
        if (result.data) {
          newStockData[result.symbol] = result.data;
        }
      });
      
      setRealTimeStocks(newStockData);
      setLastUpdated(currentDate);
      
    } catch (err) {
      console.error("Error updating real-time data:", err);
      setError("Failed to update stock data");
    } finally {
      setIsLoading(false);
    }
  };

  // Function to start auto-refresh for specific symbols
  const startAutoRefresh = (symbols, interval = 60000) => {
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current);
    }
    
    fetchLatestData(symbols);
    refreshTimerRef.current = setInterval(() => {
      fetchLatestData(symbols);
    }, interval);
    setWatchlist(symbols);
  };

  // Function to stop auto-refresh
  const stopAutoRefresh = () => {
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
  };

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current);
      }
    };
  }, []);

  // Function to get latest data for a single stock
  const getStockData = async (symbol) => {
    try {
      const data = await fetchWithLimit(`/api/stocks/${symbol.toLowerCase()}`);
      setRealTimeStocks(prev => ({
        ...prev,
        [symbol]: data.data
      }));
      return data.data;
    } catch (err) {
      console.error(`Error fetching data for ${symbol}:`, err);
      setError(`Failed to get data for ${symbol}`);
      return null;
    }
  };

  // Function to add a stock to watchlist
  const addToWatchlist = (symbol) => {
    if (!watchlist.includes(symbol)) {
      const newWatchlist = [...watchlist, symbol];
      setWatchlist(newWatchlist);
      
      if (refreshTimerRef.current) {
        startAutoRefresh(newWatchlist);
      }
      getStockData(symbol);
    }
  };

  // Function to remove a stock from watchlist
  const removeFromWatchlist = (symbol) => {
    const newWatchlist = watchlist.filter(s => s !== symbol);
    setWatchlist(newWatchlist);
    
    if (refreshTimerRef.current) {
      if (newWatchlist.length > 0) {
        startAutoRefresh(newWatchlist);
      } else {
        stopAutoRefresh();
      }
    }
  };

  const value = {
    realTimeStocks,
    lastUpdated,
    isLoading,
    error,
    watchlist,
    fetchLatestData,
    getStockData,
    startAutoRefresh,
    stopAutoRefresh,
    addToWatchlist,
    removeFromWatchlist,
    currentUser
  };

  return (
    <RealTimeDataContext.Provider value={value}>
      {children}
    </RealTimeDataContext.Provider>
  );
};
