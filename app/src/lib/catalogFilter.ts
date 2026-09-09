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
