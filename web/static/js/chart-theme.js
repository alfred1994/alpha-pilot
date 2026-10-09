export const chartColors = {
    text: '#a0aea2', line: '#2c372e', surface: '#202920', ink: '#edf2e9',
    accent: '#c0dfa1', benchmark: '#8aabbd', up: '#f29083', down: '#8ac5ad',
};

export function registerChartTheme() {
    if (!window.echarts) return;
    const axis = {
        axisLine: { lineStyle: { color: chartColors.line } },
        axisTick: { show: false },
        axisLabel: { color: chartColors.text, fontSize: 10 },
        splitLine: { lineStyle: { color: chartColors.line, type: 'dashed' } },
        nameTextStyle: { color: chartColors.text },
    };
    window.echarts.registerTheme('alphapilot', {
        color: [chartColors.accent, chartColors.benchmark, chartColors.up, chartColors.down],
        backgroundColor: 'transparent', textStyle: { color: chartColors.text, fontFamily: 'Epilogue, Microsoft YaHei, sans-serif' },
        title: { textStyle: { color: chartColors.ink, fontSize: 13, fontWeight: 400 } },
        legend: { textStyle: { color: chartColors.text, fontSize: 11 } },
        tooltip: { backgroundColor: chartColors.surface, borderColor: '#40503f', textStyle: { color: chartColors.ink, fontSize: 11 } },
        categoryAxis: axis, valueAxis: axis,
    });
}
