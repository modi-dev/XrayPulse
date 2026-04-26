// Переменные для управления таймером
let refreshTimer = null;

/**
 * Основная функция загрузки данных из API
 */
async function loadData() {
    const errorAlert = document.getElementById('error-alert');
    const errorText = document.getElementById('error-text');
    const statusIndicator = document.getElementById('status-indicator');
    
    try {
        const response = await fetch('/api/history');
        if (!response.ok) {
            const errorBody = await response.json();
            throw new Error(errorBody.error || `Ошибка сервера: ${response.status}`);
        }
        
        const data = await response.json();
        
        // Скрываем алерты, если всё прошло успешно
        errorAlert.classList.add('hidden');
        statusIndicator.innerHTML = `
            <span class="text-green-400">● Обновлено в ${new Date().toLocaleTimeString()}</span>
        `;
        
        // Вызываем функции отрисовки
        renderChart(data);
        renderTable(data);
        
    } catch (err) {
        console.error("Dashboard Error:", err);
        errorText.innerText = err.message;
        errorAlert.classList.remove('hidden');
        statusIndicator.innerHTML = '<span class="text-red-500">● Ошибка обновления</span>';
    }
}

/**
 * Отрисовка таблицы
 */
function renderTable(data) {
    const tbody = document.getElementById('tableBody');
    if (!data || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="px-4 py-8 text-center text-gray-500">Данных пока нет. Ждем фонового сбора логов...</td></tr>';
        return;
    }

    tbody.innerHTML = data.map(item => `
        <tr class="border-b border-gray-700 hover:bg-gray-750 transition">
            <td class="px-4 py-3 text-sm font-mono text-red-400">${item.type}</td>
            <td class="px-4 py-3 text-sm text-gray-300">${item.desc}</td>
            <td class="px-4 py-3 text-sm font-bold text-blue-400 text-center">${item.count}</td>
        </tr>
    `).join('');
}

/**
 * Управление интервалами обновления
 */
function setupAutoRefresh() {
    const select = document.getElementById('refreshInterval');
    
    select.addEventListener('change', (e) => {
        const interval = parseInt(e.target.value);
        
        // Очищаем старый таймер
        if (refreshTimer) clearInterval(refreshTimer);
        
        // Устанавливаем новый, если выбрано не "0"
        if (interval > 0) {
            refreshTimer = setInterval(loadData, interval);
            console.log(`Автообновление установлено на: ${interval / 1000} сек.`);
        }
    });

    // Запуск по умолчанию (например, 30 сек), если это указано в HTML
    if (select.value > 0) {
        refreshTimer = setInterval(loadData, parseInt(select.value));
    }
}

// Запуск при загрузке страницы
document.addEventListener('DOMContentLoaded', () => {
    loadData();
    setupAutoRefresh();
});