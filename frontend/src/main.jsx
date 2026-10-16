import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App.jsx';
import { AuthProvider } from './context/AuthContext.jsx';
import { NavCountsProvider } from './context/NavCountsContext.jsx';
import './styles/global.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <NavCountsProvider>
          <App />
        </NavCountsProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
