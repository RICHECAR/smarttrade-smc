import React, { useMemo } from 'react';
import {
  ComposedChart,
  Line,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceArea,
  ReferenceLine
} from 'recharts';
import { Candle, MarketStructure, OrderBlock, Pivot } from '../types';

interface ChartProps {
  data: Candle[];
  analysis: MarketStructure;
  symbol: string;
}

const ChartVisualizer: React.FC<ChartProps> = ({ data, analysis, symbol }) => {

  // Format data for Recharts
  // We simulate a candlestick by using a Bar for the body (Open-Close) 
  // and ErrorBar or just a Line for High-Low range (simplified here as range bars)
  const chartData = useMemo(() => {
    return data.map(d => ({
      ...d,
      // For color logic
      isGreen: d.close >= d.open,
      // Body range for bar
      bodyMin: Math.min(d.open, d.close),
      bodyMax: Math.max(d.open, d.close),
      bodySize: Math.abs(d.close - d.open),
      // For display
      dateStr: new Date(d.time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    }));
  }, [data]);

  const domainMin = useMemo(() => Math.min(...data.map(d => d.low)) * 0.998, [data]);
  const domainMax = useMemo(() => Math.max(...data.map(d => d.high)) * 1.002, [data]);

  // Order Blocks
  const renderOrderBlocks = () => {
    return analysis.orderBlocks.map((ob, idx) => {
      const xStart = chartData.find(c => c.time === ob.startTime)?.dateStr;
      if (!xStart) return null;

      const areaKey = `ob-${idx}`;
      const areaProps: any = {
        y1: ob.bottom,
        y2: ob.top,
        x1: xStart,
        x2: chartData[chartData.length - 1]?.dateStr,
        fill: ob.type === 'BULLISH' ? '#0ECB81' : '#F6465D',
        fillOpacity: 0.2,
        strokeOpacity: 0
      };

      const RefArea = ReferenceArea as unknown as React.ComponentType<Record<string, unknown>>;
      return (
        <React.Fragment key={areaKey}>
          <RefArea {...areaProps} />
        </React.Fragment>
      );
    });
  };

  const renderPivots = () => {
    return analysis.pivots.map((p, idx) => {
      // Only show recent pivots to avoid clutter
      if (data.length - p.index > 50) return null;
      const candle = chartData.find(c => c.time === p.time);
      if (!candle) return null;

      const lineKey = `piv-${idx}`;
      const lineProps: any = {
        y: p.price,
        stroke: p.type === 'HIGH' ? '#F6465D' : '#0ECB81',
        strokeDasharray: "3 3",
        label: {
          position: p.type === 'HIGH' ? 'top' : 'bottom',
          value: p.type === 'HIGH' ? 'PH' : 'PL',
          fill: '#6B7280',
          fontSize: 10
        }
      };

      const { y, stroke, strokeDasharray, label } = lineProps;
      return <ReferenceLine key={lineKey} y={y} stroke={stroke} strokeDasharray={strokeDasharray} label={label} />;
    });
  }

  return (
    <div className="w-full h-full bg-kraken-card rounded-lg p-4 border border-kraken-border flex flex-col">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-xl font-bold text-gray-100">{symbol} <span className="text-sm font-normal text-gray-400">Structure & Order Blocks</span></h2>
        <div className="flex gap-2 text-xs">
          <div className="flex items-center gap-1"><div className="w-3 h-3 bg-kraken-green opacity-20 border border-kraken-green"></div> Bullish OB</div>
          <div className="flex items-center gap-1"><div className="w-3 h-3 bg-kraken-red opacity-20 border border-kraken-red"></div> Bearish OB</div>
        </div>
      </div>

      <div className="flex-grow min-h-[400px] h-[500px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#2B3139" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="dateStr"
              stroke="#848E9C"
              tick={{ fontSize: 11 }}
              minTickGap={30}
            />
            <YAxis
              domain={[domainMin, domainMax]}
              orientation="right"
              stroke="#848E9C"
              tick={{ fontSize: 11 }}
              tickFormatter={(val) => val.toFixed(2)}
            />
            <Tooltip
              contentStyle={{ backgroundColor: '#1E2329', borderColor: '#2B3139', color: '#F0F0F0' }}
              itemStyle={{ color: '#F0F0F0' }}
              formatter={(value: any, name: string) => [value, name === 'close' ? 'Price' : name]}
              labelStyle={{ color: '#848E9C' }}
            />

            {/* High-Low Range Sticks (using error bar simulation or just a line connecting high low) */}
            {/* Since simple Recharts doesn't do high-low lines easily on categorical X axis without custom shapes,
                we will plot the close line and overlay OBs. For a real app, we'd use a CustomShape. 
                Here we use a Line for Close price, which is sufficient to see structure relative to OBs. */}

            <Line
              type="monotone"
              dataKey="close"
              stroke="#EAECEF"
              dot={false}
              strokeWidth={2}
            />

            {renderOrderBlocks()}
            {renderPivots()}

          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
};

export default ChartVisualizer;
