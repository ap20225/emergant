import { createContext, useContext } from 'react';
export const BountyContext = createContext(null);
export const useBounty = () => useContext(BountyContext);