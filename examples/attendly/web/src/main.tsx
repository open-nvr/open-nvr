import React from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import './services/http'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import App from './pages/App'
import Register from './pages/Register'
import VideoRegister from './pages/VideoRegister'
import Streams from './pages/Streams'
import Attendance from './pages/Attendance'
import ManageRooms from './pages/ManageRooms'
import ManageCourses from './pages/ManageCourses'
import ManageSchedule from './pages/ManageSchedule'
// import Test from './pages/Test'
import StudentData from './pages/StudentData'
import Login from './pages/Login'
import ProtectedRoute from './components/ProtectedRoute'
import ErrorBoundary from './components/ErrorBoundary'
import { ToastProvider } from './components/ToastProvider'
import { ConfirmProvider } from './components/ConfirmProvider'
const router = createBrowserRouter([
  { path: '/login', element: <Login /> },
  { 
    path: '/', 
    element: (
      <ProtectedRoute>
        <App />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/image-register', 
    element: (
      <ProtectedRoute>
        <Register />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/video-register', 
    element: (
      <ProtectedRoute>
        <VideoRegister />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/classrooms', 
    element: (
      <ProtectedRoute>
        <Streams />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/manage-rooms', 
    element: (
      <ProtectedRoute>
        <ManageRooms />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/manage-courses', 
    element: (
      <ProtectedRoute>
        <ManageCourses />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/manage-schedule', 
    element: (
      <ProtectedRoute>
        <ManageSchedule />
      </ProtectedRoute>
    ) 
  },
  { 
    path: '/attendance', 
    element: (
      <ProtectedRoute>
        <Attendance />
      </ProtectedRoute>
    ) 
  },
  // { 
  //   path: '/test', 
  //   element: (
  //     <ProtectedRoute>
  //       <Test />
  //     </ProtectedRoute>
  //   ) 
  // },
  { 
    path: '/student-data', 
    element: (
      <ProtectedRoute>
        <StudentData />
      </ProtectedRoute>
    ) 
  },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <ToastProvider>
        <ConfirmProvider>
          <RouterProvider router={router} />
        </ConfirmProvider>
      </ToastProvider>
    </ErrorBoundary>
  </React.StrictMode>
)
