import React from 'react'
import { Navigate } from 'react-router-dom'
import { authService } from '../services/auth'
import { config } from '../config/environment'

interface ProtectedRouteProps {
  children: React.ReactNode
}

const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ children }) => {
  const isAuthenticated = authService.isAuthenticated()
  const currentUser = authService.getUser()

  // Block entire app for non-admins during maintenance
  if (config.UNDER_MAINTENANCE && (!currentUser || currentUser.is_admin !== true)) {
    return (
      <div className="min-h-screen bg-yellow-50 flex flex-col justify-center py-12 sm:px-6 lg:px-8">
        <div className="sm:mx-auto sm:w-full sm:max-w-lg">
          <div className="bg-white py-8 px-6 shadow sm:rounded-lg border border-yellow-200">
            <div className="text-center">
              <div className="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-yellow-100">
                <svg className="h-6 w-6 text-yellow-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-2.5L13.73 4c-.77-.83-1.96-.83-2.73 0L3.34 16.5c-.77.83.19 2.5 1.73 2.5z" />
                </svg>
              </div>
              <h2 className="mt-6 text-lg font-medium text-gray-900">System Under Maintenance</h2>
              <p className="mt-2 text-sm text-gray-600">The system is temporarily unavailable while we perform scheduled maintenance. Please try again later.</p>
              {isAuthenticated && (
                <div className="mt-6">
                  <button
                    onClick={() => authService.logout()}
                    className="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-yellow-600 hover:bg-yellow-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-yellow-500"
                  >
                    Logout
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    )
  }
  
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }
  
  return <>{children}</>
}

export default ProtectedRoute
