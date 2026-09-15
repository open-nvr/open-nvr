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
