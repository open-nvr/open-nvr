import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authService } from '../services/auth'

const Login: React.FC = () => {
  const [formData, setFormData] = useState({
    username: '',
    password: '',
    totpCode: ''
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [needsMFA, setNeedsMFA] = useState(false)
  const navigate = useNavigate()

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFormData({
      ...formData,
      [e.target.name]: e.target.value
    })
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')

    try {
      const response = await authService.login(
        formData.username,
        formData.password,
        needsMFA ? formData.totpCode : undefined,
      )
      if (response.status === 202) {
        setNeedsMFA(true)
        setError('Enter the 6-digit code from your authenticator app')
        return
      }
      authService.setSession(response.data)
      const userResponse = await authService.me()
      authService.setUser(userResponse.data)
      navigate('/')
    } catch (err: any) {
      const d = err.response?.data?.detail
      setError(typeof d === 'string' ? d : d?.message || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen relative overflow-hidden">
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: "url('/login_bg.png')" }}
      />
      <div className="absolute inset-0 bg-black/40" />

      <div className="relative z-10 min-h-screen flex items-center justify-center md:justify-end py-12 px-4 sm:px-6 lg:px-8 md:pr-12 lg:pr-24">
        <div className="max-w-md w-full">
          <div className="bg-white/85 backdrop-blur-xl rounded-2xl shadow-2xl border border-slate-200/70 p-8">
            <div className="text-center">
              <div className="flex items-center justify-center gap-3 mb-2">
                <img src="/attendly-logo-nobg.png" alt="Attendly" className="w-10 h-10 object-contain" />
                <h1 className="text-3xl font-extrabold text-slate-900">Attendly</h1>
              </div>
              <p className="mt-1 text-slate-600">Attendance from your OpenNVR cameras</p>
              <p className="mt-1 text-xs text-slate-500">Sign in with your OpenNVR account</p>
            </div>
            <form className="mt-6 space-y-6" onSubmit={handleSubmit}>
            <div className="rounded-md shadow-sm -space-y-px">
              <div>
                <label htmlFor="username" className="sr-only">
                  Username
                </label>
                <input
                  id="username"
                  name="username"
                  type="text"
                  required
                  className="appearance-none rounded-none relative block w-full px-3 py-2 border border-slate-300 placeholder-slate-400 text-slate-900 rounded-t-md focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300 focus:z-10 sm:text-sm bg-white"
                  placeholder="Username"
                  value={formData.username}
                  onChange={handleChange}
                  disabled={loading}
                />
              </div>
              <div>
                <label htmlFor="password" className="sr-only">
                  Password
                </label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  required
                  className={`appearance-none rounded-none relative block w-full px-3 py-2 border border-slate-300 placeholder-slate-400 text-slate-900 ${
                    needsMFA ? '' : 'rounded-b-md'
                  } focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300 focus:z-10 sm:text-sm bg-white`}
                  placeholder="Password"
                  value={formData.password}
                  onChange={handleChange}
                  disabled={loading}
                />
              </div>
              {needsMFA && (
                <div>
                  <label htmlFor="totpCode" className="sr-only">
                    TOTP Code
                  </label>
                  <input
                    id="totpCode"
                    name="totpCode"
                    type="text"
                    required
                    maxLength={6}
                    className="appearance-none rounded-none relative block w-full px-3 py-2 border border-slate-300 placeholder-slate-400 text-slate-900 rounded-b-md focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-300 focus:z-10 sm:text-sm bg-white"
                    placeholder="6-digit TOTP code"
                    value={formData.totpCode}
                    onChange={handleChange}
                    disabled={loading}
                  />
                </div>
              )}
            </div>

            {error && (
              <div className="text-red-600 text-sm text-center">{error}</div>
            )}

            <div>
              <button
                type="submit"
                disabled={loading}
                className="group relative w-full flex justify-center py-2 px-4 border border-transparent text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-300 disabled:opacity-50"
              >
                {loading ? 'Signing in...' : 'Sign in'}
              </button>
            </div>

            {/* <div className="text-center">
              <Link
                to="/auth-register"
                className="text-indigo-600 hover:text-indigo-500"
              >
                Don't have an account? Sign up
              </Link>
            </div> */}
            </form>
          </div>
        </div>
      </div>
    </div>
  )
}

export default Login
