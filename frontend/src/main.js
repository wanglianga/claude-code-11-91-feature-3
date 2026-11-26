import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import DriverView from './views/DriverView.vue'
import OperationsView from './views/OperationsView.vue'
import PowerLabView from './views/PowerLabView.vue'
import './style.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/driver' },
    { path: '/driver', component: DriverView, name: 'driver' },
    { path: '/operations', component: OperationsView, name: 'operations' },
    { path: '/power-lab', component: PowerLabView, name: 'power-lab' },
  ],
})

createApp(App).use(router).mount('#app')
