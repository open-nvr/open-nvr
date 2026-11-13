/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 *
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

// Catalog search, kept out of the view so it is a pure function over
// plain data: the App Catalog runs it across two differently-shaped
// lists (installed rows and index listings) and it is the one piece of
// that page worth checking without a browser.

/** One app's searchable text, from whichever shape it arrives in — an
 *  installed row carries its blurb on the manifest, an index listing
 *  carries it flat. */
export type CatalogSearchable = {
  name?: string
  id?: string
  category?: string
  summary?: string | null
  author?: string | null
}

/** Substring match across the fields an operator would actually type:
 *  the name, the id they saw in a compose file, the blurb, the category,
 *  the author.
 *
 *  All terms must hit, so "plate alarm" narrows rather than widens —
 *  the opposite (any-term) turns a second word into a way of getting
 *  MORE results, which no one expects from a search box. */
export function matchesCatalogFilter(
  app: CatalogSearchable,
  query: string,
  category: string | null
): boolean {
  if (category && app.category !== category) return false
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  const hay = [app.name, app.id, app.category, app.summary, app.author]
    .filter(Boolean)
    .join(' ')
    .toLowerCase()
  return terms.every((t) => hay.includes(t))
}

/** How the catalog orders a group. */
export type CatalogSort = 'recommended' | 'name' | 'popular'

export type CatalogSortable = {
  name?: string
  featured?: boolean
  /** Editorial rank 0-100 set by maintainers; absent = unranked. */
  popularity?: number | null
}

/** Unranked sorts LAST under every popularity-aware order — an app
 *  nobody has ranked is not the least popular app, it is an unknown, and
 *  putting it above a ranked one would invent a comparison. */
function rank(a: CatalogSortable): number {
  return typeof a.popularity === 'number' ? a.popularity : -1
}

function byName(a: CatalogSortable, b: CatalogSortable): number {
  return (a.name ?? '').localeCompare(b.name ?? '', undefined, { sensitivity: 'base' })
}

/** A new array, ordered. Name is the final tie-break everywhere, so the
 *  grid never reshuffles between renders for equal-ranked apps. */
export function sortCatalog<T extends CatalogSortable>(
  apps: readonly T[],
  sort: CatalogSort
): T[] {
  const out = [...apps]
  if (sort === 'name') return out.sort(byName)
  if (sort === 'popular') {
    return out.sort((a, b) => rank(b) - rank(a) || byName(a, b))
  }
  // Recommended: the editorial shelf first, then rank, then name.
  return out.sort(
    (a, b) =>
      Number(Boolean(b.featured)) - Number(Boolean(a.featured)) ||
      rank(b) - rank(a) ||
      byName(a, b)
  )
}
