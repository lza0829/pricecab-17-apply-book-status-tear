<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const dash = ref(null)
const err = ref('')

onMounted(async () => {
  try {
    dash.value = await api.dashboard()
  } catch (e) {
    err.value = e.message
  }
})
</script>

<template>
  <section class="hero">
    <h1>改价单按预约时刻生效</h1>
    <p>选商品建改价单并写明生效时刻，拨业务钟或点「应用到期」后写入现行价签，再与销售夹具对账看一致与否。</p>
    <div class="chain">
      <div class="step"><strong>1 建单</strong>改价明细 + 生效时刻</div>
      <div class="step"><strong>2 拨钟 / 应用</strong>到期单写入价签</div>
      <div class="step"><strong>3 对账</strong>销售单价 vs 当时价签</div>
    </div>
  </section>

  <p v-if="err" class="msg err">{{ err }}</p>
  <p v-if="dash && !dash.apply_state.consistent" class="msg err">
    应用状态分叉：单状态 / 批次回执 / 价签本 / 历史末行不同源（详情见「生效应用」页）。
  </p>
  <div v-if="dash" class="grid">
    <div class="stat"><div class="label">业务钟</div><div class="value" style="font-size:1rem">{{ dash.business_clock }}</div></div>
    <div class="stat"><div class="label">商品</div><div class="value">{{ dash.sku_count }}</div></div>
    <div class="stat"><div class="label">改价单</div><div class="value">{{ dash.ticket_count }}</div></div>
    <div class="stat"><div class="label">待生效</div><div class="value">{{ dash.scheduled_count }}</div></div>
    <div class="stat"><div class="label">已应用（可溯源）</div><div class="value">{{ dash.applied_count }}</div></div>
    <div class="stat"><div class="label">到期待应用</div><div class="value">{{ dash.due_now }}</div></div>
  </div>
  <div v-if="dash" class="panel">
    <h2>互证</h2>
    <table>
      <tbody>
        <tr><td>价签本可按来源单溯源（票 / SKU）</td>
          <td>{{ dash.apply_state.book_source_ticket_count }} / {{ dash.book_source_count }}</td></tr>
        <tr><td>单状态号称 applied</td>
          <td>{{ dash.apply_state.claimed_applied_count }}</td></tr>
        <tr><td>分叉批次 / 未隔离分叉明细</td>
          <td>{{ dash.apply_state.divergent_batch_count }} / {{ dash.apply_state.unquarantined_divergent_line_count }}</td></tr>
        <tr><td>总览与来源可溯源一致</td>
          <td><span class="tag" :class="dash.apply_state.consistent ? 'ok' : 'bad'">
            {{ dash.apply_state.consistent ? '一致' : '分叉' }}</span></td></tr>
      </tbody>
    </table>
  </div>
</template>
