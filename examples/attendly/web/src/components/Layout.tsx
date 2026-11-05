import { Link } from 'react-router-dom'
import React, { useState, useEffect } from 'react'
import { authService, type User } from '../services/auth'
import { config } from '../config/environment'

export default function Layout({ children, title }: { children: React.ReactNode; title?: string }) {
  const [user, setUser] = useState<User | null>(null)
  const [showDropdown, setShowDropdown] = useState(false)

  useEffect(() => {
    const currentUser = authService.getUser()
    setUser(currentUser)
  }, [])

  const handleLogout = () => {
    authService.logout()
  }

  return (
    <div className="min-h-dvh bg-gray-50 flex flex-col">
      {config.UNDER_MAINTENANCE && (!user || user.is_admin !== true) && (
        <div className="w-full bg-yellow-50 border-b border-yellow-200 text-yellow-900">
          <div className="mx-auto max-w-6xl px-4 py-2 text-sm flex items-center gap-2">
            <svg className="w-4 h-4 text-yellow-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-2.5L13.73 4c-.77-.83-1.96-.83-2.73 0L3.34 16.5c-.77.83.19 2.5 1.73 2.5z" />
            </svg>
            <span>
              The system is currently under maintenance. Some features may be unavailable.
            </span>
          </div>
        </div>
      )}
      <header className="sticky top-0 z-50 border-b bg-white/70 backdrop-blur supports-[backdrop-filter]:bg-white/60 flex-shrink-0">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <Link to="/" className="flex items-center space-x-2">
            <img src="/attendly-logo-nobg.png" alt="Attendly" className="w-8 h-8 object-contain" />
            <span className="text-lg font-bold text-gray-900">Attendly</span>
          </Link>
          
          <div className="flex items-center gap-4">
            <nav className="hidden gap-4 text-sm sm:flex">
              <Link className="text-gray-700 hover:text-indigo-600" to="/image-register">Image Register</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/video-register">Video Register</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/student-data">Student Data</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/manage-rooms">Rooms</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/manage-courses">Courses</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/manage-schedule">Schedule</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/attendance">Attendance</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/gates">Gates</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/meals">Meals</Link>
              <Link className="text-gray-700 hover:text-indigo-600" to="/staff">Staff</Link>
              {/* <Link className="text-gray-700 hover:text-indigo-600" to="/test">Test</Link> */}
            </nav>
            
            {user && (
              <div className="relative">
                <button
                  onClick={() => setShowDropdown(!showDropdown)}
                  className="flex items-center gap-2 text-sm text-gray-700 hover:text-indigo-600"
                >
                  <div className="w-6 h-6 bg-indigo-600 rounded-full flex items-center justify-center">
                    <span className="text-xs text-white font-medium">
                      {user.username.charAt(0).toUpperCase()}
                    </span>
                  </div>
                  <span>{user.username}</span>
                  {user.tier && (
                    <span className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full capitalize">
                      {user.tier}
                    </span>
                  )}
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                
                {showDropdown && (
                  <div className="absolute right-0 mt-2 w-56 max-w-[90vw] bg-white rounded-md shadow-lg border z-50 overflow-hidden">
                    <div className="py-1">
                      <div className="px-4 py-2 text-sm text-gray-500 border-b break-all">
                        {user.email || user.username}
                        <div className="text-xs text-gray-400 mt-0.5">Signed in with OpenNVR</div>
                      </div>
                      <button
                        onClick={handleLogout}
                        className="block w-full text-left px-4 py-2 text-sm text-red-600 hover:bg-gray-100"
                      >
                        Logout
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-2 flex-1 overflow-hidden w-full">
        {title && <h1 className="mb-4 text-2xl font-bold tracking-tight">{title}</h1>}
        {children}
      </main>
    </div>
  )
}
