import { createRootRoute, createRoute, createRouter, Outlet, redirect } from '@tanstack/react-router'
import { LoginPage } from './routes/LoginPage'
import { SearchPage } from './routes/SearchPage'
import { ArticlePage } from './routes/ArticlePage'

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

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: requireAuth,
  component: SearchPage,
})

export const articleRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/article',
  beforeLoad: requireAuth,
  validateSearch: (
    search: Record<string, string>,
  ): { doi?: string; zotero_key?: string; url?: string } => ({
    doi: search.doi,
    zotero_key: search.zotero_key,
    url: search.url,
  }),
  component: ArticlePage,
})

const routeTree = rootRoute.addChildren([loginRoute, indexRoute, articleRoute])

export const router = createRouter({
  routeTree,
  basepath: '/webapp',
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
