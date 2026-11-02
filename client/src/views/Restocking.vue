<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div class="card budget-card">
      <div class="budget-row">
        <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
        <span class="budget-value">{{ money(budget) }}</span>
      </div>
      <input
        id="budget-slider"
        v-model.number="budget"
        type="range"
        min="0"
        :max="BUDGET_MAX"
        :step="BUDGET_STEP"
        class="budget-slider"
      />
      <div class="budget-scale">
        <span>{{ money(0) }}</span>
        <span>{{ t('restocking.budgetMax') }} {{ money(BUDGET_MAX) }}</span>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="orderError" class="error">{{ orderError }}</div>
    <div v-if="confirmation" class="confirmation" role="status">{{ confirmation }}</div>

    <div v-if="loading && !result" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="result" :class="{ refreshing: loading }">
      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.totalCost') }}</div>
          <div class="stat-value">{{ money(result.total_cost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ money(result.remaining_budget) }}</div>
        </div>
        <div class="stat-card warning">
          <div class="stat-label">{{ t('restocking.orderLeadTime') }}</div>
          <div class="stat-value">{{ orderLeadTime === null ? '-' : t('restocking.days', { days: orderLeadTime }) }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendations') }} ({{ items.length }})</h3>
          <button
            type="button"
            class="place-order-btn"
            :disabled="!canPlaceOrder"
            @click="placeOrder"
          >
            {{ submitting ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="items.length === 0" class="empty-state">{{ t('restocking.empty') }}</div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.name') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th class="num">{{ t('restocking.table.shortfall') }}</th>
                <th class="num">{{ t('restocking.table.quantity') }}</th>
                <th class="num">{{ t('restocking.table.unitCost') }}</th>
                <th class="num">{{ t('restocking.table.lineTotal') }}</th>
                <th class="num">{{ t('restocking.table.leadTime') }}</th>
                <th>{{ t('restocking.table.coverage') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in items" :key="item.sku">
                <td><strong>{{ item.sku }}</strong></td>
                <td>{{ translateProductName(item.name) }}</td>
                <td>{{ t(`trends.${item.trend}`) }}</td>
                <td class="num">{{ item.shortfall }}</td>
                <td class="num">{{ item.quantity }}</td>
                <td class="num">{{ money(item.unit_cost, 2) }}</td>
                <td class="num"><strong>{{ money(item.line_total, 2) }}</strong></td>
                <td class="num">{{ item.lead_time_days }}</td>
                <td>
                  <span v-if="!item.fully_covered" class="badge warning" :title="t('restocking.partialHint')">
                    {{ t('restocking.partial') }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrencyWithDecimals } from '../utils/currency'

// Total needed cost is ~$27,963; round up to the next step for the slider max
const BUDGET_STEP = 500
const BUDGET_MAX = 28000
const DEBOUNCE_MS = 250

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const budget = ref(5000)
    const result = ref(null)
    const loading = ref(false)
    const error = ref(null)
    const submitting = ref(false)
    const orderError = ref(null)
    const confirmation = ref(null)

    // True while a slider change is waiting on the debounce timer
    const pending = ref(false)
    let debounceTimer = null
    // Monotonic id so responses arriving out of order are ignored
    let requestSeq = 0

    const items = computed(() => (result.value ? result.value.items : []))

    // Order lead time is driven by the slowest item
    const orderLeadTime = computed(() => {
      if (items.value.length === 0) return null
      return Math.max(...items.value.map(i => i.lead_time_days))
    })

    // Only allow ordering when the displayed items match the current slider value
    const canPlaceOrder = computed(() =>
      items.value.length > 0 && !submitting.value && !loading.value && !pending.value
    )

    const money = (amount, decimals = 0) =>
      formatCurrencyWithDecimals(Number(amount) || 0, currentCurrency.value, decimals)

    const loadRecommendations = async () => {
      const seq = ++requestSeq
      pending.value = false
      loading.value = true
      error.value = null
      try {
        const data = await api.getRestockingRecommendations(budget.value)
        if (seq !== requestSeq) return // a newer request superseded this one
        result.value = data
      } catch (err) {
        if (seq !== requestSeq) return
        error.value = t('restocking.loadError') + ': ' + err.message
      } finally {
        if (seq === requestSeq) loading.value = false
      }
    }

    watch(budget, () => {
      confirmation.value = null
      orderError.value = null
      pending.value = true
      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(loadRecommendations, DEBOUNCE_MS)
    })

    const placeOrder = async () => {
      // Guard against double submit even if the button state lags
      if (!canPlaceOrder.value) return
      submitting.value = true
      orderError.value = null
      confirmation.value = null
      try {
        const order = await api.createRestockingOrder({
          items: items.value.map(i => ({ sku: i.sku, quantity: i.quantity })),
          budget: budget.value
        })
        // Reset budget to 0; the budget watcher clears messages, so set the
        // confirmation after the watcher has run (next tick)
        budget.value = 0
        await nextTick()
        confirmation.value = t('restocking.orderSuccess', {
          orderNumber: order.order_number,
          days: order.lead_time_days
        })
      } catch (err) {
        const detail = err.response?.data?.detail
        orderError.value = t('restocking.orderError') + ': ' + (typeof detail === 'string' ? detail : err.message)
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadRecommendations)
    onBeforeUnmount(() => {
      clearTimeout(debounceTimer)
      requestSeq++ // drop any in-flight response
    })

    return {
      t,
      BUDGET_MAX,
      BUDGET_STEP,
      budget,
      result,
      items,
      loading,
      error,
      submitting,
      orderError,
      confirmation,
      orderLeadTime,
      canPlaceOrder,
      money,
      placeOrder,
      translateProductName
    }
  }
}
</script>

<style scoped>
.budget-card {
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.5rem;
}

.budget-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  width: 100%;
  accent-color: #0f172a;
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: #64748b;
  margin-top: 0.25rem;
}

.refreshing {
  opacity: 0.6;
  transition: opacity 0.15s;
}

.num {
  text-align: right;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.5rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #e2e8f0;
  color: #64748b;
  cursor: not-allowed;
}

.confirmation {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
  border-radius: 8px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
  font-size: 0.875rem;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
  font-size: 0.875rem;
}

.error {
  margin-bottom: 1rem;
}
</style>
