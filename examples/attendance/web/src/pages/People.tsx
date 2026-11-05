// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Student Data: everyone registered, searchable, with what can
// be done to each — more photos, an edit, consent withdrawn, removal.

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Camera, Pencil, Search, ShieldOff, Trash2, UserPlus, Users } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { api, groupName, type Person } from '../attendance'
import { Avatar } from '../components/faces'
import { useFeedback } from '../components/feedback'
import { Badge, Button, Card, Empty, ErrorNote, Field, Loading, Modal, PageTitle, RowMenu, Table, inlineInputClass, inputClass, menuItem, td, th } from '../components/ui'
import { useGroups, useLoad } from '../hooks'

const PAGE = 50

export default function People() {
  const { can } = useAuth()
  const { toast, confirm } = useFeedback()
  const groups = useGroups()
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  const [groupId, setGroupId] = useState('')
  const [offset, setOffset] = useState(0)
  const [editing, setEditing] = useState<Person | null>(null)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setTerm(q.trim())
      setOffset(0)
    }, 300)
    return () => window.clearTimeout(t)
  }, [q])

  const people = useLoad(
    () => api.listPeople({ q: term || undefined, group_id: groupId ? Number(groupId) : undefined, limit: PAGE, offset }),
    [term, groupId, offset],
  )

  const act = async (fn: () => Promise<unknown>, done: string) => {
    try {
      await fn()
      toast('success', done)
      people.reload()
    } catch (e) {
      toast('error', errorText(e))
    }
  }

  const withdraw = async (p: Person) => {
    const ok = await confirm({
      title: 'Withdraw consent?',
      body: `${p.display_name}'s face photos are deleted now, and they are no longer recognised until consent is given again and their face re-enrolled. Their past attendance is kept.`,
      confirm: 'Withdraw and delete faces',
      danger: true,
    })
    if (ok) act(() => api.setConsent(p.id, false), `Consent withdrawn; ${p.display_name}'s faces deleted`)
  }

  const forget = async (p: Person) => {
    const ok = await confirm({
      title: 'Delete their face photos?',
      body: `The ${p.face_samples} enrolled photos of ${p.display_name} are deleted. Consent stays on record, so you can enrol new ones.`,
      confirm: 'Delete photos',
      danger: true,
    })
    if (ok) act(() => api.forgetFaces(p.id), `${p.display_name}'s face photos deleted`)
  }

  const remove = async (p: Person) => {
    const ok = await confirm({
      title: `Remove ${p.display_name}?`,
      body: 'They leave every group and their face photos are deleted. Their past attendance stays in the reports.',
      confirm: 'Remove',
      danger: true,
    })
    if (ok) act(() => api.removePerson(p.id), `${p.display_name} removed`)
  }

  const total = people.data?.total ?? 0

  return (
    <>
      <PageTitle
        title="People"
        subtitle="Everyone registered for attendance."
        actions={
          can.enroll && (
            <Link to="/register" className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-indigo-700">
              <UserPlus className="h-4 w-4" /> Register someone
            </Link>
          )
        }
      />
      <Card>
        <div className="flex flex-wrap items-center gap-3 border-b p-3">
          <div className="relative min-w-[14rem] flex-1">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
            <input className={`${inputClass} pl-9`} placeholder="Search by name, ID or email" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <select
            className={`${inlineInputClass} min-w-[12rem]`}
            value={groupId}
            onChange={(e) => {
              setGroupId(e.target.value)
              setOffset(0)
            }}
            aria-label="Group"
          >
            <option value="">Every group</option>
            {(groups.data ?? []).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
          <span className="text-sm text-gray-500">{people.data ? `${total} ${total === 1 ? 'person' : 'people'}` : ''}</span>
        </div>

        {people.error ? (
          <div className="p-4">
            <ErrorNote message={people.error} onRetry={people.reload} />
          </div>
        ) : !people.data ? (
          <Loading />
        ) : !people.data.people.length ? (
          <Empty icon={<Users className="h-10 w-10" />} title={term || groupId ? 'Nobody matches' : 'Nobody registered yet'}>
            {!term && !groupId && can.enroll && (
              <Link to="/register" className="text-indigo-700 underline">
                Register the first person
              </Link>
            )}
          </Empty>
        ) : (
          <Table>
            <thead className="bg-gray-50">
              <tr>
                <th className={th}>Name</th>
                <th className={th}>ID</th>
                <th className={th}>Groups</th>
                <th className={th}>Face photos</th>
                <th className={th}>Consent</th>
                <th className={th}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {people.data.people.map((p) => (
                <tr key={p.id} className="hover:bg-gray-50">
                  <td className={td}>
                    <div className="flex items-center gap-3">
                      <Avatar person={p} />
                      <div className="min-w-0">
                        <div className="font-medium text-gray-900">{p.display_name}</div>
                        {p.email && <div className="truncate text-xs text-gray-500">{p.email}</div>}
                      </div>
                    </div>
                  </td>
                  <td className={`${td} whitespace-nowrap`}>{p.external_ref ?? '—'}</td>
                  <td className={td}>
                    <div className="flex flex-wrap gap-1">
                      {p.group_ids.length ? (
                        p.group_ids.map((g) => (
                          <Badge key={g} tone="indigo">
                            {groupName(groups.data, g)}
                          </Badge>
                        ))
                      ) : (
                        <span className="text-gray-400">None</span>
                      )}
                    </div>
                  </td>
                  <td className={td}>
                    <Badge tone={p.face_samples ? 'green' : 'amber'}>{p.face_samples}</Badge>
                  </td>
                  <td className={td}>{p.consent_at ? <Badge tone="green">Given</Badge> : <Badge tone="gray">None</Badge>}</td>
                  <td className={`${td} text-right`}>
                    {can.enroll && (
                      <RowMenu label={`Actions for ${p.display_name}`}>
                        {(close) => (
                          <>
                            <Link className={menuItem} to={`/register?person=${p.id}`} onClick={close}>
                              <Camera className="h-4 w-4" /> Add face photos
                            </Link>
                            <button className={menuItem} onClick={() => (close(), setEditing(p))}>
                              <Pencil className="h-4 w-4" /> Edit details
                            </button>
                            {p.face_samples > 0 && (
                              <button className={menuItem} onClick={() => (close(), forget(p))}>
                                <Trash2 className="h-4 w-4" /> Delete face photos
                              </button>
                            )}
                            {p.consent_at && (
                              <button className={`${menuItem} text-red-600`} onClick={() => (close(), withdraw(p))}>
                                <ShieldOff className="h-4 w-4" /> Withdraw consent
                              </button>
                            )}
                            <button className={`${menuItem} text-red-600`} onClick={() => (close(), remove(p))}>
                              <Trash2 className="h-4 w-4" /> Remove
                            </button>
                          </>
                        )}
                      </RowMenu>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}

        {total > PAGE && (
          <div className="flex items-center justify-between border-t px-4 py-3 text-sm text-gray-600">
            <span>
              {offset + 1}–{Math.min(offset + PAGE, total)} of {total}
            </span>
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                Previous
              </Button>
              <Button variant="secondary" size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
                Next
              </Button>
            </div>
          </div>
        )}
      </Card>

      {editing && (
        <EditPerson
          person={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            people.reload()
          }}
        />
      )}
    </>
  )
}

function EditPerson({ person, onClose, onSaved }: { person: Person; onClose: () => void; onSaved: () => void }) {
  const { toast } = useFeedback()
  const [name, setName] = useState(person.display_name)
  const [ref, setRef] = useState(person.external_ref ?? '')
  const [email, setEmail] = useState(person.email ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.updatePerson(person.id, { display_name: name.trim(), external_ref: ref.trim() || null, email: email.trim() || null })
      toast('success', 'Saved')
      onSaved()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Edit ${person.display_name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} busy={busy} disabled={!name.trim()}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Full name">
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
        </Field>
        <Field label="ID">
          <input className={inputClass} value={ref} onChange={(e) => setRef(e.target.value)} maxLength={100} />
        </Field>
        <Field label="Email">
          <input className={inputClass} type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {error && <ErrorNote message={error} />}
      </div>
    </Modal>
  )
}
