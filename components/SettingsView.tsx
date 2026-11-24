import React, { useState } from 'react';
import { Eye, EyeOff, Save, AlertTriangle, Shield, X } from 'lucide-react';
import { ApiConfig } from '../types';

interface SettingsProps {
  config: ApiConfig;
  onSave: (config: ApiConfig) => void;
  onCancel: () => void;
}

const SettingsView: React.FC<SettingsProps> = ({ config, onSave, onCancel }) => {
  const [formData, setFormData] = useState<ApiConfig>(config);
  const [showSecret, setShowSecret] = useState(false);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type, checked } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value
    }));
  };

  const isConfigComplete = formData.apiKey && formData.secretKey && formData.passphrase;

  return (
    <div className="lg:col-span-4 bg-kraken-card border border-kraken-border rounded-lg p-6 max-w-2xl mx-auto w-full">
      <div className="flex items-center justify-between mb-8 pb-4 border-b border-kraken-border">
        <h2 className="text-2xl font-bold text-white flex items-center gap-2">
          <Shield className="text-kraken-cyan" /> 
          API Configuration
        </h2>
        <button onClick={onCancel} className="text-gray-400 hover:text-white">
            <X size={24} />
        </button>
      </div>

      <div className="space-y-6">
        {/* Warning Banner */}
        {formData.liveMode && (
          <div className="bg-red-900/20 border border-kraken-red/50 rounded-lg p-4 flex gap-3 items-start">
            <AlertTriangle className="text-kraken-red shrink-0" size={24} />
            <div>
              <h3 className="font-bold text-kraken-red">Live Trading Enabled</h3>
              <p className="text-sm text-gray-300 mt-1">
                The application will execute real orders on your Kraken Futures account using the provided credentials. 
                Ensure your API key permissions are correctly set (Futures Trade).
              </p>
            </div>
          </div>
        )}

        {/* Form Fields */}
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">API Key</label>
            <input
              type="text"
              name="apiKey"
              value={formData.apiKey}
              onChange={handleChange}
              className="w-full bg-[#0B0E11] border border-kraken-border rounded p-3 text-white focus:border-kraken-cyan focus:outline-none transition-colors"
              placeholder="Enter API Key"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Secret Key</label>
            <div className="relative">
              <input
                type={showSecret ? "text" : "password"}
                name="secretKey"
                value={formData.secretKey}
                onChange={handleChange}
                className="w-full bg-[#0B0E11] border border-kraken-border rounded p-3 text-white focus:border-kraken-cyan focus:outline-none transition-colors pr-10"
                placeholder="Enter API Secret Key"
              />
              <button
                type="button"
                onClick={() => setShowSecret(!showSecret)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-white"
              >
                {showSecret ? <EyeOff size={18} /> : <Eye size={18} />}
              </button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Passphrase</label>
            <input
              type="password"
              name="passphrase"
              value={formData.passphrase}
              onChange={handleChange}
              className="w-full bg-[#0B0E11] border border-kraken-border rounded p-3 text-white focus:border-kraken-cyan focus:outline-none transition-colors"
              placeholder="Enter API Passphrase"
            />
          </div>
        </div>

        {/* Toggle Live Mode */}
        <div className="pt-6 border-t border-kraken-border">
            <label className="flex items-center justify-between cursor-pointer group">
                <div className="flex flex-col">
                    <span className="font-medium text-white group-hover:text-kraken-cyan transition-colors">Enable Live Trading</span>
                    <span className="text-xs text-gray-500">Allow automated order execution</span>
                </div>
                <div className="relative">
                    <input 
                        type="checkbox" 
                        name="liveMode" 
                        checked={formData.liveMode} 
                        onChange={handleChange}
                        className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-kraken-red"></div>
                </div>
            </label>
        </div>

        {/* Actions */}
        <div className="pt-6 flex gap-4 justify-end">
            <button 
                onClick={onCancel}
                className="px-6 py-2 rounded text-gray-300 hover:text-white hover:bg-kraken-border transition-colors"
            >
                Cancel
            </button>
            <button
                onClick={() => onSave(formData)}
                disabled={formData.liveMode && !isConfigComplete}
                className={`flex items-center gap-2 px-6 py-2 rounded font-medium transition-colors ${
                    formData.liveMode 
                        ? 'bg-kraken-red hover:bg-red-600 text-white' 
                        : 'bg-kraken-cyan hover:bg-[#00d0dd] text-black'
                } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
                <Save size={18} />
                Save Configuration
            </button>
        </div>
      </div>
    </div>
  );
};

export default SettingsView;
