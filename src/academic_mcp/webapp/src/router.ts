import { createRootRoute, createRoute, createRouter, Outlet, redirect } from '@tanstack/react-router'
import { LoginPage } from './routes/LoginPage'
import { SearchPage } from './routes/SearchPage'
import { ArticlePage } from './routes/ArticlePage'
import { HealthPage } from './routes/HealthPage'

function requireAuth() {
  if (localStorage.getItem('wa_logged_in') !== '1') {
    throw redirect({ to: '/login' })
  }
}

const rootRoute = createRootRoute({ component: Outlet })

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: LoginPage,
})

export interface SearchPageParams {
  q?: string
  semantic?: boolean
  zotero_only?: boolean
  include_scite?: boolean
  domain_hint?: string
}

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: requireAuth,
  validateSearch: (search: Record<string, unknown>): SearchPageParams => ({
    q: typeof search.q === 'string' ? search.q : undefined,
    semantic: search.semantic === true || search.semantic === 'true' || undefined,
    zotero_only: search.zotero_only === true || search.zotero_only === 'true' || undefined,
    include_scite: search.include_scite === true || search.include_scite === 'true' || undefined,
    domain_hint: typeof search.domain_hint === 'string' ? search.domain_hint : undefined,
  }),
  component: SearchPage,
})

export const articleRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/article',
  beforeLoad: requireAuth,
  validateSearch: (
    search: Record<string, string>,
  ): { doi?: string; zotero_key?: string; url?: string; q?: string } => ({
    doi: search.doi,
    zotero_key: search.zotero_key,
    url: search.url,
    q: search.q,
  }),
  component: ArticlePage,
})

const healthRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/health',
  beforeLoad: requireAuth,
  component: HealthPage,
})

const routeTree = rootRoute.addChildren([loginRoute, indexRoute, articleRoute, healthRoute])

export const router = createRouter({
  routeTree,
  basepath: '/webapp',
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
