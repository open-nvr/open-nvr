import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { Users, Image as ImageIcon, BarChart3, UserPlus, Video, Database, ClipboardList, Building2, BookOpen, CalendarDays, DoorOpen, UtensilsCrossed, BriefcaseBusiness, CalendarCheck } from 'lucide-react'
import axios from 'axios'
import Layout from '../components/Layout'
import { config } from '../config/environment'

const Card = ({ to, title, desc, icon: Icon, className = '' }: { to: string; title: string; desc: string; icon: React.ComponentType<any>; className?: string }) => (
  <Link
    to={to}
    className={`group block rounded-xl border border-gray-200 bg-white p-6 shadow-sm transition-all duration-200 ease-out hover:shadow-lg hover:border-indigo-200 hover:bg-gradient-to-b hover:from-white hover:to-indigo-50/40 hover:-translate-y-0.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 ${className}`}
  >
    <div className="flex items-center justify-between">
      <div className="flex items-center">
        <Icon className="w-6 h-6 text-gray-600 transition-colors duration-200 group-hover:text-indigo-600" />
        <h3 className="text-lg font-semibold text-gray-900 ml-2">{title}</h3>
      </div>
      <span className="text-indigo-600 opacity-0 translate-x-0 transition-all duration-200 group-hover:opacity-100 group-hover:translate-x-1">→</span>
    </div>
    <p className="mt-2 text-sm text-gray-600">{desc}</p>
  </Link>
)

export default function App() {
  const [stats, setStats] = useState<any>(null)
  const [statsLoading, setStatsLoading] = useState(false)
  const [statsError, setStatsError] = useState('')

  const API = config.API_BASE_URL
  const STATS_CACHE_KEY = 'attendly:dashboard:stats'

  useEffect(() => {
    let cancelled = false
    setStatsLoading(true)
    try {
      const cachedRaw = localStorage.getItem(STATS_CACHE_KEY)
      if (cachedRaw) {
        const cached = JSON.parse(cachedRaw)
        if (!cancelled) setStats(cached?.data ?? cached)
      }
    } catch {}
    const loadStats = async () => {
      try {
        if (!cancelled) setStatsLoading(true)
        setStatsError('')
        const res = await axios.get(`${API}/registry/stats?include_face_ids=false&include_students=false`)
        if (!cancelled) {
          setStats(res.data)
          try {
            localStorage.setItem(STATS_CACHE_KEY, JSON.stringify({ data: res.data, fetchedAt: Date.now() }))
          } catch {}
        }
      } catch (e) {
        if (!cancelled) setStatsError('Failed to load statistics')
      } finally {
        if (!cancelled) setStatsLoading(false)
      }
    }
    loadStats()
    const id = setInterval(loadStats, 30000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return (
    <Layout title="Dashboard">
      <div className="mb-2">
        <p className="text-gray-600">Welcome to Attendly — Simple, reliable attendance tracking</p>
      </div>

      {/* Registry statistics */}
      <div className="mb-8">
        <div className="bg-white rounded-lg shadow-sm border p-2">
          {/* <div className="flex items-center justify-between mb-4"> */}
            {/* <h3 className="text-lg font-semibold text-gray-900">System Statistics</h3> */}
          {/* </div> */}
          {statsError && <div className="text-sm text-red-600">{statsError}</div>}
          {stats ? (
            <div className="grid gap-4 md:grid-cols-3">
              <div className="relative bg-blue-50 border border-blue-200 p-4 rounded-lg">
                {statsLoading && (
                  <div className="absolute top-2 right-2 flex items-center" title="Updating…">
                    <div className="h-3 w-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></div>
                    <span className="sr-only">Updating…</span>
                  </div>
                )}
                <div className="flex items-center text-blue-600">
                  <Users className="w-6 h-6" />
                  <div className="text-2xl font-bold ml-2">
                  {(stats.statistics?.total_students_with_faces ?? '-')}{typeof stats?.statistics?.total_students_with_faces === 'number' && Number(stats?.statistics?.max_students) > 0 ? ` / ${stats.statistics.max_students}` : ''}
                  </div>
                </div>
                <div className="text-sm text-gray-600 mt-1">Registered Students</div>
              </div>
              <div className="relative bg-green-50 border border-green-200 p-4 rounded-lg">
                {statsLoading && (
                  <div className="absolute top-2 right-2 flex items-center" title="Updating…">
                    <div className="h-3 w-3 border-2 border-green-500 border-t-transparent rounded-full animate-spin"></div>
                    <span className="sr-only">Updating…</span>
                  </div>
                )}
                <div className="flex items-center text-green-600">
                  <ImageIcon className="w-6 h-6" />
                  <div className="text-2xl font-bold ml-2">{stats.statistics?.total_faces ?? '-'}</div>
                </div>
                <div className="text-sm text-gray-600 mt-1">Total Images</div>
              </div>
              <div className="relative bg-purple-50 border border-purple-200 p-4 rounded-lg">
                {statsLoading && (
                  <div className="absolute top-2 right-2 flex items-center" title="Updating…">
                    <div className="h-3 w-3 border-2 border-purple-500 border-t-transparent rounded-full animate-spin"></div>
                    <span className="sr-only">Updating…</span>
                  </div>
                )}
                <div className="flex items-center text-purple-600">
                  <BarChart3 className="w-6 h-6" />
                  <div className="text-2xl font-bold ml-2">{stats.statistics?.average_faces_per_student ?? '-'}</div>
                </div>
                <div className="text-sm text-gray-600 mt-1">Avg Images/Student</div>
              </div>
            </div>
          ) : (
            !statsLoading && <div className="text-gray-500">Statistics unavailable</div>
          )}
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <Card to="/image-register" title="Image Register" desc="Register new students with face recognition" icon={UserPlus} />
        <Card to="/video-register" title="Video Register" desc="Register students using webcam video capture" icon={Video} />
        <Card to="/student-data" title="Student Data" desc="View and manage student data" icon={Database} />
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 mt-2">
        <Card to="/manage-rooms" title="Manage Rooms" desc="Add/update Rooms Details" icon={Building2} />
        <Card to="/manage-courses" title="Manage Courses" desc="Create courses and sections" icon={BookOpen} />
        <Card to="/manage-schedule" title="Manage Schedule" desc="Plan weekly class schedule" icon={CalendarDays} />
        <Card to="/attendance" title="Attendance" desc="Monitor student attendance records" icon={ClipboardList} />
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 mt-2">
        <Card to="/gates" title="Gates" desc="Hostel and mess flap barriers, live decisions" icon={DoorOpen} />
        <Card to="/meals" title="Meal Bookings" desc="Who has booked which meal; the mess gate checks these" icon={UtensilsCrossed} />
        <Card to="/staff" title="Staff" desc="Enrol staff and set department cameras" icon={BriefcaseBusiness} />
        <Card to="/staff-attendance" title="Staff Attendance" desc="Daily check-in and check-out per department" icon={CalendarCheck} />
      </div>
    </Layout>
  )
}
