import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { translations as englishTranslations } from './locales/en'
import { translations as frenchTranslations } from './locales/fr'
import type { Language, TranslationCatalog } from './locales/types'

export type { Language, TranslationCatalog } from './locales/types'

const STORAGE_KEY = 'opennvr.language'

const translations: Record<Language, TranslationCatalog> = {
  en: englishTranslations,
  fr: frenchTranslations,
}

function getInitialLanguage(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'en' || stored === 'fr') return stored
  } catch {
    // Storage may be unavailable in private browsing.
  }

  return navigator.language.toLowerCase().startsWith('fr') ? 'fr' : 'en'
}

type I18nContextValue = {
  language: Language
  setLanguage: (language: Language) => void
  t: (key: string, values?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(getInitialLanguage)
  const setLanguage = (next: Language) => setLanguageState(next)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, language)
    } catch {
      // Storage may be unavailable in private browsing.
    }
    document.documentElement.lang = language
  }, [language])

  const value = useMemo<I18nContextValue>(() => ({
    language,
    setLanguage,
    t: (key, values) => {
      const text = translations[language][key] ?? translations.en[key] ?? key
      return values ? text.replace(/\{\{(\w+)\}\}/g, (_, name: string) => String(values[name] ?? `{{${name}}}`)) : text
    },
  }), [language])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useTranslation() {
  const context = useContext(I18nContext)
  if (!context) throw new Error('useTranslation must be used inside I18nProvider')
  return context
}

export function getDateLocale(language: Language) {
  return language === 'fr' ? 'fr-FR' : 'en-US'
}

/**
 * Dates and times in the language the operator picked.
 *
 * `getDateLocale` has been exported since the catalogues landed and was
 * never called once: every `toLocaleTimeString([], …)` in the app passes
 * an empty locale list, which means "whatever the BROWSER is set to".
 * So an operator who selects Français still reads `02:02 PM` — a format
 * France does not use — because their Chrome is American. The language
 * switcher translates the labels around the timestamp and not the
 * timestamp itself.
 *
 * These helpers close that. They take anything a timestamp arrives as
 * (ISO string, epoch ms, Date, or nothing at all) so call sites stop
 * wrapping everything in `new Date(...)`, and an unparseable value
 * returns the em dash rather than `Invalid Date`.
 */
const _d = (value: string | number | Date | null | undefined): Date | null => {
  if (value === null || value === undefined || value === '') return null
  const d = value instanceof Date ? value : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export type DateFormatters = {
  /** Locale BCP-47 tag, for the rare caller that needs Intl directly. */
  locale: string
  time: (v: string | number | Date | null | undefined, o?: Intl.DateTimeFormatOptions) => string
  date: (v: string | number | Date | null | undefined, o?: Intl.DateTimeFormatOptions) => string
  dateTime: (v: string | number | Date | null | undefined, o?: Intl.DateTimeFormatOptions) => string
}

/** Build formatters for a language. Use `useDateFormat()` inside a
 *  component; this exists for the module-level helpers that cannot call
 *  a hook and must be handed the language instead. */
export function dateFormatters(language: Language): DateFormatters {
  const locale = getDateLocale(language)
  return {
    locale,
    time: (v, o) => _d(v)?.toLocaleTimeString(
      locale, o ?? { hour: '2-digit', minute: '2-digit' }) ?? '—',
    date: (v, o) => _d(v)?.toLocaleDateString(locale, o) ?? '—',
    dateTime: (v, o) => _d(v)?.toLocaleString(locale, o) ?? '—',
  }
}

export function useDateFormat(): DateFormatters {
  const { language } = useTranslation()
  return useMemo(() => dateFormatters(language), [language])
}
